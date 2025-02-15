from __future__ import annotations

import asyncio
import contextlib
import logging
import traceback

from livekit import rtc

from .. import apipe, ipc_enc, utils
from ..job_context import JobContext, _ShutdownInfo
from ..job_request import AutoSubscribe
from ..log import logger
from ..utils import time_ms
from . import protocol


class LogHandler(logging.Handler):
    """Log handler forwarding logs to the worker process"""

    def __init__(self, writer: ipc_enc.ProcessPipeWriter) -> None:
        super().__init__(logging.NOTSET)
        self._writer = writer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            try:
                msg = record.getMessage()
            except TypeError:
                if record.args:
                    msg = record.msg.format(*record.args)
                else:
                    msg = record.msg

            if record.exc_info:
                type, value, tb = record.exc_info
                msg += "\n" + "".join(traceback.format_exception(type, value, tb))

            ipc_enc.write_msg(
                self._writer,
                protocol.Log(
                    level=record.levelno, logger_name=record.name, message=msg
                ),
            )
        except Exception as e:
            print(f"failed to log {record.filename}:{record.lineno}, exception '{e}'")


async def _start(
    pipe: apipe.AsyncPipe, args: protocol.JobMainArgs, room: rtc.Room
) -> None:
    utils.http_context._new_session_ctx()

    # used by the JobContext to signal shutdown
    close_tx = close_rx = utils.aio.Chan[_ShutdownInfo]()

    auto_subscribe = args.accept_data.auto_subscribe
    opts = rtc.RoomOptions(auto_subscribe=auto_subscribe == AutoSubscribe.SUBSCRIBE_ALL)

    cnt = room.connect(args.url, args.token, options=opts)
    start_req: protocol.StartJobRequest | None = None
    usertask: asyncio.Task | None = None
    shutting_down = False

    async def _start_if_valid():
        nonlocal usertask

        if not start_req or not room.isconnected():
            return

        if auto_subscribe == AutoSubscribe.SUBSCRIBE_NONE:
            return

        @room.on("track_published")
        def on_track_published(
            pub: rtc.RemoteTrackPublication, participant: rtc.RemoteParticipant
        ):
            if (
                pub.kind == rtc.TrackKind.KIND_AUDIO
                and auto_subscribe == AutoSubscribe.AUDIO_ONLY
            ):
                pub.set_subscribed(True)
            elif (
                pub.kind == rtc.TrackKind.KIND_VIDEO
                and auto_subscribe == AutoSubscribe.VIDEO_ONLY
            ):
                pub.set_subscribed(True)

        for participant in room.participants.values():
            for track_pub in participant.tracks.values():
                if (
                    track_pub.kind == rtc.TrackKind.KIND_AUDIO
                    and auto_subscribe == AutoSubscribe.AUDIO_ONLY
                ):
                    track_pub.set_subscribed(True)
                elif (
                    track_pub.kind == rtc.TrackKind.KIND_VIDEO
                    and auto_subscribe == AutoSubscribe.VIDEO_ONLY
                ):
                    track_pub.set_subscribed(True)

        ctx = JobContext(close_tx, start_req.job, room)
        usertask = asyncio.create_task(args.accept_data.entry(ctx))

        def log_exception(t: asyncio.Task) -> None:
            if not t.cancelled() and t.exception():
                logger.error(
                    f"unhandled exception in the job entry {args.accept_data.entry}",
                    exc_info=t.exception(),
                )

        usertask.add_done_callback(log_exception)
        await pipe.write(protocol.StartJobResponse())

    @room.on("disconnected")
    def on_disconnected():
        close_tx.send_nowait(_ShutdownInfo(reason="room disconnected"))

    select = utils.aio.select([pipe, cnt, close_rx])
    try:
        while True:
            s = await select()
            if s.selected is cnt:
                if s.exc:
                    error = "".join(traceback.format_exception_only(type(s.exc), s.exc))
                    await pipe.write(protocol.StartJobResponse(error=error))
                    break  # failed to connect, break and exit the process
                await _start_if_valid()

            if s.selected is close_rx:
                await pipe.write(protocol.UserExit(reason=s.value.reason))
                break

            msg = s.result()
            if isinstance(msg, protocol.ShutdownRequest):
                shutting_down = True
                break
            if isinstance(msg, protocol.StartJobRequest):
                start_req = msg
                await _start_if_valid()
            if isinstance(msg, protocol.Ping):
                last_timestamp = msg.timestamp
                await pipe.write(
                    protocol.Pong(last_timestamp=last_timestamp, timestamp=time_ms())
                )
    finally:
        await select.aclose()

    logger.debug("disconnecting from room")
    await room.disconnect()

    with contextlib.suppress(Exception):
        # exceptions are already logged inside the done_callback
        if usertask is not None:
            await usertask  # type: ignore

    await utils.http_context._close_http_ctx()

    if shutting_down:
        await pipe.write(protocol.ShutdownResponse())


def _run_job(cch: ipc_enc.ProcessPipe, args: protocol.JobMainArgs) -> None:
    """Entry point for a job process"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    logging.root.setLevel(logging.NOTSET)
    # logging.root.propagate = False
    logging.root.addHandler(LogHandler(cch))

    # current process pid
    logger.debug(
        "process started",
        extra={"url": args.url},
    )

    pipe = apipe.AsyncPipe(cch, loop, protocol.IPC_MESSAGES)
    loop.slow_callback_duration = 0.05  # 50ms
    utils.aio.debug.hook_slow_callbacks(2)  # start warning after 2s
    loop.set_debug(args.asyncio_debug)

    room = rtc.Room(loop=loop)
    main_task = loop.create_task(_start(pipe, args, room))

    try:
        loop.run_until_complete(main_task)
    except KeyboardInterrupt:
        # ignore
        loop.run_until_complete(main_task)
