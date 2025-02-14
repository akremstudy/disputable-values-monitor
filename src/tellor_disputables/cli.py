"""CLI dashboard to display recent values reported to Tellor oracles."""
import logging
import warnings
from time import sleep

import click
import pandas as pd
from telliot_core.apps.telliot_config import TelliotConfig
from telliot_core.cli.utils import async_run
from telliot_feeds.cli.utils import build_query

from tellor_disputables import WAIT_PERIOD
from tellor_disputables.alerts import alert
from tellor_disputables.alerts import generic_alert
from tellor_disputables.alerts import get_twilio_info
from tellor_disputables.data import chain_events
from tellor_disputables.data import get_events
from tellor_disputables.data import parse_new_report_event
from tellor_disputables.utils import clear_console
from tellor_disputables.utils import get_tx_explorer_url
from tellor_disputables.utils import Topics

warnings.simplefilter("ignore", UserWarning)
logger = logging.getLogger("telliot_feeds.sources.price_aggregator")
logger.handlers = [h for h in logger.handlers if not isinstance(h, logging.StreamHandler)]
logging.basicConfig(filename="log.txt", level=logging.INFO, format="%(asctime)s %(message)s")


def print_title_info() -> None:
    """Prints the title info."""
    click.echo("Disputable Values Monitor 📒🔎📲")


@click.command()
@click.option(
    "-a", "--all-values", is_flag=True, default=False, show_default=True, help="if set, get alerts for all values"
)
@click.option("-w", "--wait", help="how long to wait between checks", type=int, default=WAIT_PERIOD)
@click.option("-f", "--filter", help="build a queryId and get alerts for that queryId only", is_flag=True)
@click.option(
    "-c",
    "--confidence-threshold",
    help="percent difference threshold for notifications (a float between 0 and 1)",
    type=float,
    default=0.05,
)
@async_run
async def main(all_values: bool, wait: int, filter: bool, confidence_threshold: float) -> None:
    """CLI dashboard to display recent values reported to Tellor oracles."""
    await start(all_values=all_values, wait=wait, filter=filter, confidence_threshold=confidence_threshold)


async def start(all_values: bool, wait: int, filter: bool, confidence_threshold: float) -> None:
    """Start the CLI dashboard."""
    print_title_info()
    from_number, recipients = get_twilio_info()
    if from_number is None or recipients is None:
        logging.error("Missing phone numbers. See README for required environment variables. Exiting.")
        return

    display_rows = []
    displayed_events = set()

    # Build query if filter is set
    query_id = build_query().query_id.hex() if filter else None

    while True:

        cfg = TelliotConfig()
        # Fetch NewReport events
        event_lists = await get_events(
            cfg=cfg,
            contract_name="tellor360-oracle",
            topics=[Topics.NEW_REPORT] + [f"0x{query_id}"] if query_id is not None else [],
            wait=wait,
        )
        tellor360_events = await chain_events(
            cfg=cfg,
            chain_addy={
                1: "0xD3b9A1DCAbd16c482785Fd4265cB4580B84cdeD7",
                5: "0xD3b9A1DCAbd16c482785Fd4265cB4580B84cdeD7",
            },
            topics=[Topics.NEW_ORACLE_ADDRESS, Topics.NEW_PROPOSED_ORACLE_ADDRESS],
            wait=wait,
        )
        event_lists += tellor360_events
        for event_list in event_lists:
            # event_list = [(80001, EXAMPLE_NEW_REPORT_EVENT)]
            if not event_list:
                continue
            for chain_id, event in event_list:

                cfg.main.chain_id = chain_id
                if Topics.NEW_ORACLE_ADDRESS in event.topics or Topics.NEW_PROPOSED_ORACLE_ADDRESS in event.topics:
                    link = get_tx_explorer_url(cfg=cfg, tx_hash=event.transactionHash.hex())
                    msg = f"\n❗NEW ORACLE ADDRESS ALERT❗\n{link}"
                    generic_alert(from_number=from_number, recipients=recipients, msg=msg)
                    continue

                try:
                    new_report = await parse_new_report_event(
                        cfg=cfg, confidence_threshold=confidence_threshold, log=event
                    )
                except Exception as e:
                    logging.error("unable to parse new report event! " + str(e))
                    continue

                # Skip duplicate & missing events
                if new_report is None or new_report.tx_hash in displayed_events:
                    continue
                displayed_events.add(new_report.tx_hash)

                # Refesh
                clear_console()
                print_title_info()

                alert(all_values, new_report, recipients, from_number)

                display_rows.append(
                    (
                        new_report.tx_hash,
                        new_report.eastern_time,
                        new_report.link,
                        new_report.query_type,
                        new_report.value,
                        new_report.status_str,
                        new_report.asset,
                        new_report.currency,
                    )
                )

                # Prune display
                if len(display_rows) > 10:
                    displayed_events.remove(display_rows[0][0])
                    del display_rows[0]

                # Display table
                _, times, links, _, values, disputable_strs, assets, currencies = zip(*display_rows)
                dataframe_state = dict(
                    When=times,
                    Transaction=links,
                    # QueryType=query_types,
                    Asset=assets,
                    Currency=currencies,
                    Value=values,
                    Disputable=disputable_strs,
                )
                df = pd.DataFrame.from_dict(dataframe_state)
                print(df.to_markdown(), end="\r")
                df.to_csv("table.csv", mode="a", header=False)

        sleep(wait)


if __name__ == "__main__":
    main()
