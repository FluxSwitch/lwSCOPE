from dataclasses import dataclass
from typing import Any, Dict, Optional

@dataclass
class HSDataSource:
    signals: Dict[str, float]
    sequence_num: int

'''
Here are the defined HSData push========================================
Only GUI <-- Logic
    HSDataSource(
        signals=, # Dict[str, float]
        sequence_num=  # int
    )
========================================================================
'''


@dataclass
class UIMsg:
    msg_ID: int
    msg_type: str
    msg_subtype: str
    payload: Optional[Any] = None

'''
Here are the defined UIMsg
=
=
=
[UI start button]=======================================================
Step 1: GUI --> Logic
    UIMsg(
        msg_ID=,  # to be set by sender
        msg_type="START_BUTTON",
        msg_subtype="REQUEST",
        payload="SWITCH_TO_START,<bitrate>,<frame_format>" or "SWITCH_TO_STOP"
        # Example: "SWITCH_TO_START,115200,8N1" or "SWITCH_TO_STOP"
    )
    
Step 2: GUI <-- Logic
    UIMsg(
        msg_ID=,  # same as received
        msg_type="START_BUTTON",
        msg_subtype="RESPONSE",
        payload="SUCCESS" or "FAIL"
    )
========================================================================
=
=
=
[Get log(polled)]=======================================================
Step 1: GUI --> Logic
    UIMsg(
        msg_ID=,  # to be set by sender
        msg_type="GET_LOG",
        msg_subtype="REQUEST",
        payload=None
    )
    
Step 2: GUI <-- Logic
    if got log:
        UIMsg(
            msg_ID=,  # same as received
            msg_type="GET_LOG",
            msg_subtype="RESPONSE",
            payload= log content (str)
        )
        
    if no log:
        UIMsg(
            msg_ID=,  # same as received
            msg_type="GET_LOG",
            msg_subtype="NO_LOG",
            payload=None
        )
========================================================================
=
=
=
[Get COM port list]=======================================================
Step 1: GUI --> Logic
    UIMsg(
        msg_ID=,  # to be set by sender
        msg_type="GET_COM_PORT_LIST",
        msg_subtype="REQUEST",
        payload=None
    )
    
Step 2: GUI <-- Logic
    UIMsg(
        msg_ID=,  # same as received
        msg_type="GET_COM_PORT_LIST",
        msg_subtype="RESPONSE",
        payload= list of COM ports (List[str])
    )
========================================================================
=
=
=
[Set COM port]=======================================================
Step 1: GUI --> Logic
    UIMsg(
        msg_ID=,  # to be set by sender
        msg_type="SET_COM_PORT",
        msg_subtype="REQUEST",
        payload=Name of COM port (str)
    )
    
Step 2: GUI <-- Logic
    UIMsg(
        msg_ID=,  # same as received
        msg_type="SET_COM_PORT",
        msg_subtype="RESPONSE",
        payload="SUCCESS" or "FAIL"
    )
========================================================================
=
=
=
[Get param read value (polled per row)]===================================
Step 1: GUI --> Logic
    UIMsg(
        msg_ID=,  # to be set by sender
        msg_type="GET_PR_VALUE",
        msg_subtype="REQUEST",
        payload= "<addr>,<type>"  # e.g. "0x0001,U16"
        # type: U8, S8, U16, S16, U32, S32, Float32
    )
    
Step 2: GUI <-- Logic
    UIMsg(
        msg_ID=,  # same as received
        msg_type="GET_PR_VALUE",
        msg_subtype="RESPONSE",
        payload= "<addr>,<value>"  # e.g. "0x0001,12345"
    )
    
    Note: Logic may NOT respond at all if serial device is unresponsive.
    GUI uses 200ms per-tick timeout; after 5 timeouts the row is marked
    dark-red and polling advances to the next row.
========================================================================
=
=
=
[Set param write value (per row Send button)]=============================
Step 1: GUI --> Logic
    UIMsg(
        msg_ID=,  # to be set by sender
        msg_type="SET_PR_VALUE",
        msg_subtype="REQUEST",
        payload= "<addr>,<type>,<value>"  # e.g. "0x0001,U16,12345"
        # type: U8, S8, U16, S16, U32, S32, Float32
    )
    
Step 2: GUI <-- Logic
    UIMsg(
        msg_ID=,  # same as received
        msg_type="SET_PR_VALUE",
        msg_subtype="RESPONSE",
        payload="SUCCESS" or "FAIL"
    )
========================================================================
=
=
=
[Get protocol stats (polled)]============================================
Step 1: GUI --> Logic
    UIMsg(
        msg_ID=,  # to be set by sender
        msg_type="GET_PROTOCOL_STATS",
        msg_subtype="REQUEST",
        payload=None
    )
    
Step 2: GUI <-- Logic
    UIMsg(
        msg_ID=,  # same as received
        msg_type="GET_PROTOCOL_STATS",
        msg_subtype="RESPONSE",
        payload= {
            "total_success_received_packet": int,
            "total_success_transmitted_packet": int,
            "total_success_received_log_packet": int,
            "total_success_received_ds_packet": int,
            "dropped_packet": int,
            "crc_error": int,
            "invalid_packet": int,
        }
    )
========================================================================

[Clear protocol stats]===================================================
Step 1: GUI --> Logic
    UIMsg(
        msg_ID=,  # to be set by sender
        msg_type="CLEAR_PROTOCOL_STATS",
        msg_subtype="REQUEST",
        payload=None
    )
    (No response — fire and forget)
========================================================================

'''
