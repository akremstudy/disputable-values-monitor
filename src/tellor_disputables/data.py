"""Get and parse NewReport events from Tellor oracles."""
import asyncio
import logging
from typing import Any
from typing import List
from typing import Optional
from typing import Tuple
from typing import Union

from telliot_core.apps.telliot_config import TelliotConfig
from telliot_core.directory import contract_directory
from telliot_feeds.datafeed import DataFeed
from telliot_feeds.queries.abi_query import AbiQuery
from telliot_feeds.queries.json_query import JsonQuery
from telliot_feeds.queries.price.spot_price import SpotPrice
from urllib3.exceptions import MaxRetryError
from urllib3.exceptions import NewConnectionError
from web3 import Web3
from web3._utils.events import get_event_data
from web3.types import LogReceipt

from tellor_disputables import DATAFEED_LOOKUP
from tellor_disputables import NEW_REPORT_ABI
from tellor_disputables import WAIT_PERIOD
from tellor_disputables.utils import disputable_str
from tellor_disputables.utils import get_tx_explorer_url
from tellor_disputables.utils import NewReport


def get_contract_info(chain_id: int, name: str) -> Tuple[Optional[str], Optional[str]]:
    """Get the contract address and ABI for the given chain ID."""
    contracts = contract_directory.find(chain_id=chain_id, name=name)

    if len(contracts) > 0:
        contract_info = contracts[0]
        addr = contract_info.address[chain_id]
        abi = contract_info.get_abi(chain_id=chain_id)
        return addr, abi

    else:
        logging.info(f"Could not find contract info for chain_id {chain_id}")
        return None, None


def mk_filter(
    from_block: int, to_block: Union[str, int], addr: str, topics: list[str]
) -> dict[str, Union[int, str, list[str]]]:
    """Create a dict with the given parameters."""
    return {
        "fromBlock": from_block,
        "toBlock": to_block,
        "address": addr,
        "topics": topics,
    }


async def log_loop(web3: Web3, addr: str, topics: list[str], wait: int) -> list[tuple[int, Any]]:
    """Generate a list of recent events from a contract."""
    # go back 20 blocks; 10 for possible reorgs, the other 10 should cover for even the fastest chains. block/sec
    # 1000 is the max number of blocks that can be queried at once
    blocks = min(20 * (wait / WAIT_PERIOD), 1000)
    try:
        block_number = web3.eth.get_block_number()
    except Exception as e:
        if "server rejected" in str(e):
            logging.info("Attempted to connect to deprecated infura network. Please check configs!" + str(e))
        else:
            logging.warning("unable to retrieve latest block number:" + str(e))
        return []

    event_filter = mk_filter(block_number - int(blocks), "latest", addr, topics)

    try:
        events = web3.eth.get_logs(event_filter)  # type: ignore
    except (MaxRetryError, NewConnectionError, ValueError) as e:
        msg = str(e)
        if "unknown block" in msg:
            logging.error("waiting for new blocks")
        elif "request failed or timed out" in msg:
            logging.error("request for eth event logs failed")
        else:
            logging.error("unknown RPC error gathering eth event logs \n" + msg)
        return []

    unique_events_list = []
    for event in events:
        if (web3.eth.chain_id, event) not in unique_events_list:
            unique_events_list.append((web3.eth.chain_id, event))

    return unique_events_list


async def general_fetch_new_datapoint(feed: DataFeed) -> Optional[Any]:
    """Fetch a new datapoint from a datafeed."""
    return await feed.source.fetch_new_datapoint()


async def is_disputable(reported_val: float, query_id: str, conf_threshold: float = 0.05) -> Optional[bool]:
    """Check if the reported value is disputable."""
    if reported_val is None:
        logging.error("Need reported value to check disputability")
        return None

    if query_id not in DATAFEED_LOOKUP:
        logging.info(f"new report for unsupported query ID: {query_id}")
        return None

    current_feed: DataFeed[Any] = DATAFEED_LOOKUP[query_id]
    trusted_val, _ = await general_fetch_new_datapoint(current_feed)
    if trusted_val is not None:
        percent_diff = (reported_val - trusted_val) / trusted_val
        return float(abs(percent_diff)) > conf_threshold
    else:
        logging.error("Unable to fetch new datapoint from feed")
        return None


async def chain_events(
    cfg: TelliotConfig, chain_addy: dict[int, str], topics: list[str], wait: int
) -> List[List[tuple[int, Any]]]:
    """"""
    events_loop = []
    for chain_id, address in chain_addy.items():
        try:
            endpoint = cfg.endpoints.find(chain_id=chain_id)[0]
            if endpoint.url.endswith("{INFURA_API_KEY}"):
                continue
            endpoint.connect()
            w3 = endpoint.web3
        except (IndexError, ValueError) as e:
            logging.error(f"Unable to connect to endpoint on chain_id {chain_id}: " + str(e))
            continue
        events_loop.append(log_loop(w3, address, topics, wait))
    events: List[List[tuple[int, Any]]] = await asyncio.gather(*events_loop)

    return events


async def get_events(
    cfg: TelliotConfig, contract_name: str, topics: list[str], wait: int
) -> List[List[tuple[int, Any]]]:
    """Get all events from all live Tellor networks"""

    log_loops = []

    for endpoint in cfg.endpoints.endpoints:
        if endpoint.url.endswith("{INFURA_API_KEY}"):
            continue
        try:
            endpoint.connect()
        except Exception as e:
            logging.warning("unable to connect to endpoint: " + str(e))

        w3 = endpoint.web3

        if not w3:
            continue

        addr, _ = get_contract_info(endpoint.chain_id, contract_name)

        if not addr:
            continue

        log_loops.append(log_loop(w3, addr, topics, wait))

    events_lists: List[List[tuple[int, Any]]] = await asyncio.gather(*log_loops)

    return events_lists


def get_query_from_data(query_data: bytes) -> Optional[Union[AbiQuery, JsonQuery]]:
    for q_type in (JsonQuery, AbiQuery):
        try:
            return q_type.get_query_from_data(query_data)
        except ValueError:
            pass
    return None


async def parse_new_report_event(
    cfg: TelliotConfig,
    confidence_threshold: float,
    log: LogReceipt,
) -> Optional[NewReport]:
    """Parse a NewReport event."""

    chain_id = cfg.main.chain_id
    endpoint = cfg.endpoints.find(chain_id=chain_id)[0]

    if not endpoint:
        logging.error(f"Unable to find a suitable endpoint for chain_id {chain_id}")
        return None
    else:

        try:
            endpoint.connect()
            w3 = endpoint.web3
        except ValueError as e:
            logging.error(f"Unable to connect to endpoint on chain_id {chain_id}: " + str(e))
            return None

        codec = w3.codec
        event_data = get_event_data(codec, NEW_REPORT_ABI, log)

    query_id = event_data.args._queryId.hex()

    tx_hash = event_data.transactionHash.hex()
    q = get_query_from_data(event_data.args._queryData)
    if isinstance(q, SpotPrice):
        asset = q.asset.upper()
        currency = q.currency.upper()
    else:
        logging.error("unsupported query type")
        return None

    val = q.value_type.decode(event_data.args._value)
    link = get_tx_explorer_url(tx_hash=tx_hash, cfg=cfg)
    disputable = await is_disputable(val, query_id, confidence_threshold)
    if disputable is None:
        logging.info("unable to check disputability")
        return None
    else:
        status_str = disputable_str(disputable, query_id)

        return NewReport(
            chain_id=w3.eth.chain_id,
            eastern_time=event_data.args._time,
            tx_hash=tx_hash,
            link=link,
            query_type=type(q).__name__,
            value=val,
            asset=asset,
            currency=currency,
            query_id=query_id,
            disputable=disputable,
            status_str=status_str,
        )
