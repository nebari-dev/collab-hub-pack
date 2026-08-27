from __future__ import annotations

import logging
import re

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from collab_hub_api.config import ConnectorsConfig
from collab_hub_api.connectors.calendar_client import (
    CalendarSearchError,
    CalendarUpstreamError,
    GoogleCalendarClient,
)
from collab_hub_api.connectors.drive_client import DriveUpstreamError, GoogleDriveClient, UnsupportedDriveFileType
from collab_hub_api.connectors.gmail_client import GmailClient, GmailSearchError, GmailUpstreamError
from collab_hub_api.connectors.google_tokens import (
    ConnectorNotConnected,
    ConnectorReconnectRequired,
    ConnectorTokenError,
    GoogleTokenProvider,
)
from collab_hub_api.connectors.models import (
    DRIVE_READONLY_SCOPE,
    GMAIL_READONLY_SCOPE,
    GOOGLE_CALENDAR_READONLY_SCOPE,
    SLACK_READONLY_SCOPES,
    CalendarReadRequest,
    CalendarReadResponse,
    CalendarSearchRequest,
    CalendarSearchResponse,
    ConnectorSummary,
    DriveReadRequest,
    DriveReadResponse,
    DriveSearchRequest,
    DriveSearchResponse,
    GmailReadRequest,
    GmailReadResponse,
    GmailSearchRequest,
    GmailSearchResponse,
    GmailStatus,
    GoogleCalendarStatus,
    GoogleDriveStatus,
    SlackChannelsResponse,
    SlackDmsResponse,
    SlackReadRequest,
    SlackReadResponse,
    SlackSearchRequest,
    SlackSearchResponse,
    SlackStatus,
    SlackThreadReadRequest,
    SlackThreadReadResponse,
)
from collab_hub_api.connectors.slack_client import (
    SLACK_TOKEN_INVALID_ERRORS,
    SlackClient,
    SlackConversationNotAllowed,
    SlackUpstreamError,
)
from collab_hub_api.connectors.slack_tokens import SlackTokenProvider
from collab_hub_api.frames.auth import get_auth_context

router = APIRouter(prefix="/connectors", tags=["connectors"])
logger = logging.getLogger("frames_server.connectors")


def get_connectors_config(request: Request) -> ConnectorsConfig:
    return request.app.state.connectors_config


@router.get("", response_model=list[ConnectorSummary])
async def list_connectors(
    request: Request,
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> list[ConnectorSummary]:
    return [
        await _google_drive_status(request, config),
        await _gmail_status(request, config),
        await _google_calendar_status(request, config),
        await _slack_status(request, config),
    ]


@router.get("/google-drive/status", response_model=GoogleDriveStatus)
async def google_drive_status(
    request: Request,
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> GoogleDriveStatus:
    return await _google_drive_status(request, config)


@router.post("/google-drive/search", response_model=DriveSearchResponse)
async def search_google_drive(
    body: DriveSearchRequest,
    request: Request,
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> DriveSearchResponse:
    client = await _drive_client(request, config)
    try:
        files = await client.search(
            query=body.query,
            limit=body.limit,
            modified_after=body.modified_after,
            mime_types=body.mime_types,
        )
    except DriveUpstreamError as exc:
        logger.info(
            "google_drive_search_upstream_error",
            extra={"operation": exc.operation, "status_code": exc.status_code},
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Google Drive search failed") from exc
    return DriveSearchResponse(files=files)


@router.post("/google-drive/files/{file_id}/read", response_model=DriveReadResponse)
async def read_google_drive_file(
    file_id: str,
    body: DriveReadRequest,
    request: Request,
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> DriveReadResponse:
    client = await _drive_client(request, config)
    try:
        file = await client.metadata(file_id)
        text, truncated = await client.read_text(file, body.max_chars)
    except UnsupportedDriveFileType as exc:
        return DriveReadResponse(
            file=file,
            text="",
            truncated=False,
            unsupported=True,
            unsupported_reason=str(exc),
        )
    except DriveUpstreamError as exc:
        logger.info(
            "google_drive_read_upstream_error",
            extra={"operation": exc.operation, "status_code": exc.status_code, "file_id": file_id},
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Google Drive file read failed") from exc
    return DriveReadResponse(file=file, text=text, truncated=truncated)


@router.get("/gmail/status", response_model=GmailStatus)
async def gmail_status(
    request: Request,
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> GmailStatus:
    return await _gmail_status(request, config)


@router.post("/gmail/search", response_model=GmailSearchResponse)
async def search_gmail(
    body: GmailSearchRequest,
    request: Request,
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> GmailSearchResponse:
    client = await _gmail_client(request, config)
    try:
        messages, next_page_token, result_size_estimate = await client.search(
            query=body.query,
            limit=body.limit,
            label_ids=body.label_ids,
            include_spam_trash=body.include_spam_trash,
            days_back=body.days_back,
            since_date=body.since_date,
            until_date=body.until_date,
            time_zone=body.time_zone,
            page_token=body.page_token,
        )
    except GmailSearchError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except GmailUpstreamError as exc:
        logger.info("gmail_search_upstream_error", extra={"operation": exc.operation, "status_code": exc.status_code})
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Gmail search failed") from exc
    return GmailSearchResponse(
        messages=messages,
        next_page_token=next_page_token,
        result_size_estimate=result_size_estimate,
    )


@router.post("/gmail/messages/{message_id}/read", response_model=GmailReadResponse)
async def read_gmail_message(
    message_id: str,
    body: GmailReadRequest,
    request: Request,
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> GmailReadResponse:
    client = await _gmail_client(request, config)
    try:
        message, text, truncated, body_format, attachment_count = await client.read(message_id, body.max_chars)
    except GmailUpstreamError as exc:
        logger.info("gmail_read_upstream_error", extra={"operation": exc.operation, "status_code": exc.status_code})
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Gmail message read failed") from exc
    return GmailReadResponse(
        message=message,
        text=text,
        truncated=truncated,
        body_format=body_format,
        has_attachments=attachment_count > 0,
        attachment_count=attachment_count,
    )


@router.get("/google-calendar/status", response_model=GoogleCalendarStatus)
async def google_calendar_status(
    request: Request,
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> GoogleCalendarStatus:
    return await _google_calendar_status(request, config)


@router.post("/google-calendar/search", response_model=CalendarSearchResponse)
async def search_google_calendar(
    body: CalendarSearchRequest,
    request: Request,
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> CalendarSearchResponse:
    client = await _google_calendar_client(request, config)
    try:
        events, next_cursor = await client.search(
            query=body.query,
            limit=body.limit,
            calendar_ids=body.calendar_ids,
            time_min=body.time_min,
            time_max=body.time_max,
            days_back=body.days_back,
            days_ahead=body.days_ahead,
            since_date=body.since_date,
            until_date=body.until_date,
            time_zone=body.time_zone,
            cursor=body.cursor,
        )
    except CalendarSearchError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except CalendarUpstreamError as exc:
        logger.info(
            "google_calendar_search_upstream_error",
            extra={"operation": exc.operation, "status_code": exc.status_code},
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Google Calendar search failed") from exc
    return CalendarSearchResponse(events=events, next_cursor=next_cursor)


@router.post(
    "/google-calendar/calendars/{calendar_id}/events/{event_id}/read",
    response_model=CalendarReadResponse,
)
async def read_google_calendar_event(
    calendar_id: str,
    event_id: str,
    body: CalendarReadRequest,
    request: Request,
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> CalendarReadResponse:
    client = await _google_calendar_client(request, config)
    try:
        event, truncated = await client.read(calendar_id, event_id, body.max_chars)
    except CalendarUpstreamError as exc:
        logger.info(
            "google_calendar_read_upstream_error",
            extra={"operation": exc.operation, "status_code": exc.status_code},
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Google Calendar event read failed") from exc
    return CalendarReadResponse(event=event, truncated=truncated)


@router.get("/slack/status", response_model=SlackStatus)
async def slack_status(
    request: Request,
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> SlackStatus:
    return await _slack_status(request, config)


@router.get("/slack/channels", response_model=SlackChannelsResponse)
async def list_slack_channels(
    request: Request,
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str = Query(default="", max_length=4096),
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> SlackChannelsResponse:
    client = await _slack_client(request, config)
    try:
        channels, next_cursor = await client.list_channels(limit=limit, cursor=cursor)
    except SlackUpstreamError as exc:
        logger.info(
            "slack_channels_upstream_error",
            extra={"operation": exc.operation, "status_code": exc.status_code},
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Slack channel list failed") from exc
    return SlackChannelsResponse(channels=channels, next_cursor=next_cursor)


@router.get("/slack/dms", response_model=SlackDmsResponse)
async def list_slack_dms(
    request: Request,
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str = Query(default="", max_length=256),
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> SlackDmsResponse:
    client = await _slack_client(request, config)
    try:
        dms, next_cursor = await client.list_dms(limit=limit, cursor=cursor)
    except SlackUpstreamError as exc:
        logger.info(
            "slack_dms_upstream_error",
            extra={"operation": exc.operation, "status_code": exc.status_code},
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Slack DM list failed") from exc
    return SlackDmsResponse(dms=dms, next_cursor=next_cursor)


@router.post("/slack/search", response_model=SlackSearchResponse)
async def search_slack(
    body: SlackSearchRequest,
    request: Request,
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> SlackSearchResponse:
    client = await _slack_client(request, config)
    try:
        hits, next_page = await client.search_page(query=body.query, limit=body.limit, page=body.page)
    except SlackUpstreamError as exc:
        logger.info(
            "slack_search_upstream_error",
            extra={"operation": exc.operation, "status_code": exc.status_code},
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Slack search failed") from exc
    return SlackSearchResponse(hits=hits, next_page=next_page)


@router.post("/slack/channels/{channel_id}/read", response_model=SlackReadResponse)
async def read_slack_conversation(
    channel_id: str,
    body: SlackReadRequest,
    request: Request,
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> SlackReadResponse:
    _validate_slack_channel_id(channel_id)
    client = await _slack_client(request, config)
    try:
        messages, has_more, next_cursor = await client.read_conversation(
            channel_id=channel_id,
            limit=body.limit,
            oldest=body.oldest,
            latest=body.latest,
            cursor=body.cursor,
        )
    except SlackConversationNotAllowed as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except SlackUpstreamError as exc:
        logger.info(
            "slack_read_upstream_error",
            extra={"operation": exc.operation, "status_code": exc.status_code, "channel_id": channel_id},
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Slack conversation read failed") from exc
    return SlackReadResponse(channel_id=channel_id, messages=messages, has_more=has_more, next_cursor=next_cursor)


@router.post("/slack/channels/{channel_id}/threads/{message_ts}/read", response_model=SlackThreadReadResponse)
async def read_slack_thread(
    channel_id: str,
    message_ts: str,
    body: SlackThreadReadRequest,
    request: Request,
    _auth=Depends(get_auth_context),
    config: ConnectorsConfig = Depends(get_connectors_config),
) -> SlackThreadReadResponse:
    _validate_slack_channel_id(channel_id)
    _validate_slack_message_ts(message_ts)
    client = await _slack_client(request, config)
    try:
        messages, has_more, next_cursor = await client.read_thread(
            channel_id=channel_id,
            message_ts=message_ts,
            limit=body.limit,
            cursor=body.cursor,
        )
    except SlackConversationNotAllowed as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except SlackUpstreamError as exc:
        logger.info(
            "slack_thread_upstream_error",
            extra={"operation": exc.operation, "status_code": exc.status_code, "channel_id": channel_id},
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Slack thread read failed") from exc
    return SlackThreadReadResponse(
        channel_id=channel_id,
        message_ts=message_ts,
        messages=messages,
        has_more=has_more,
        next_cursor=next_cursor,
    )


async def _google_drive_status(request: Request, config: ConnectorsConfig) -> GoogleDriveStatus:
    provider = GoogleTokenProvider(config.google)
    try:
        await provider.access_token(request)
    except ConnectorTokenError as exc:
        return GoogleDriveStatus(
            connected=False,
            state=exc.state,
            scopes=[],
            detail=str(exc),
        )
    return GoogleDriveStatus(
        connected=True,
        state="connected",
        scopes=[DRIVE_READONLY_SCOPE],
    )


async def _drive_client(request: Request, config: ConnectorsConfig) -> GoogleDriveClient:
    token = await _google_connector_token(request, config)
    return GoogleDriveClient(
        access_token=token,
        api_base_url=config.google.drive_api_base_url,
        timeout_seconds=config.google.request_timeout_seconds,
    )


async def _gmail_status(request: Request, config: ConnectorsConfig) -> GmailStatus:
    provider = GoogleTokenProvider(config.google)
    try:
        token = await provider.access_token(request)
    except ConnectorTokenError as exc:
        return GmailStatus(connected=False, state=exc.state, scopes=[], detail=str(exc))
    client = _new_gmail_client(token, config)
    try:
        await client.verify_access()
    except GmailUpstreamError as exc:
        state = "reconnect_required" if exc.status_code in {401, 403} else "unavailable"
        return GmailStatus(connected=False, state=state, scopes=[], detail=str(exc))
    except httpx.HTTPError:
        return GmailStatus(
            connected=False,
            state="unavailable",
            scopes=[],
            detail="Gmail API access check failed.",
        )
    return GmailStatus(connected=True, state="connected", scopes=[GMAIL_READONLY_SCOPE])


def _new_gmail_client(token: str, config: ConnectorsConfig) -> GmailClient:
    return GmailClient(
        access_token=token,
        api_base_url=config.google.gmail_api_base_url,
        timeout_seconds=config.google.request_timeout_seconds,
    )


async def _gmail_client(request: Request, config: ConnectorsConfig) -> GmailClient:
    token = await _google_connector_token(request, config)
    return _new_gmail_client(token, config)


async def _google_calendar_status(request: Request, config: ConnectorsConfig) -> GoogleCalendarStatus:
    provider = GoogleTokenProvider(config.google)
    try:
        token = await provider.access_token(request)
    except ConnectorTokenError as exc:
        return GoogleCalendarStatus(connected=False, state=exc.state, scopes=[], detail=str(exc))
    client = _new_google_calendar_client(token, config)
    try:
        await client.verify_access()
    except CalendarUpstreamError as exc:
        state = "reconnect_required" if exc.status_code in {401, 403} else "unavailable"
        return GoogleCalendarStatus(connected=False, state=state, scopes=[], detail=str(exc))
    except httpx.HTTPError:
        return GoogleCalendarStatus(
            connected=False,
            state="unavailable",
            scopes=[],
            detail="Google Calendar API access check failed.",
        )
    return GoogleCalendarStatus(
        connected=True,
        state="connected",
        scopes=[GOOGLE_CALENDAR_READONLY_SCOPE],
    )


def _new_google_calendar_client(token: str, config: ConnectorsConfig) -> GoogleCalendarClient:
    return GoogleCalendarClient(
        access_token=token,
        api_base_url=config.google.calendar_api_base_url,
        timeout_seconds=config.google.request_timeout_seconds,
    )


async def _google_calendar_client(request: Request, config: ConnectorsConfig) -> GoogleCalendarClient:
    token = await _google_connector_token(request, config)
    return _new_google_calendar_client(token, config)


async def _google_connector_token(request: Request, config: ConnectorsConfig) -> str:
    provider = GoogleTokenProvider(config.google)
    try:
        return await provider.access_token(request)
    except ConnectorNotConnected as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ConnectorReconnectRequired as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ConnectorTokenError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc


async def _slack_status(request: Request, config: ConnectorsConfig) -> SlackStatus:
    provider = SlackTokenProvider(config.slack)
    try:
        token = await provider.access_token(request)
    except ConnectorTokenError as exc:
        return SlackStatus(
            connected=False,
            state=exc.state,
            scopes=[],
            detail=str(exc),
        )

    # A brokered token is not proof of a usable connector: Keycloak can broker a
    # Slack OpenID sign-in/identity token that links successfully yet fails every
    # Web API read. Validate it against auth.test so that case reports
    # reconnect_required here instead of silently 502-ing on the first read.
    client = _new_slack_client(token, config)
    try:
        access = await client.verify_access()
    except SlackUpstreamError as exc:
        if exc.message in SLACK_TOKEN_INVALID_ERRORS:
            return SlackStatus(
                connected=False,
                state="reconnect_required",
                scopes=[],
                detail=(
                    "Slack is linked but the brokered token is not a usable Slack Web API user token "
                    f"(auth.test returned '{exc.message}'). Reconnect Slack and make sure Keycloak brokers a "
                    "Slack user token with read scopes (search:read and the *:history scopes), not an OpenID "
                    "sign-in token."
                ),
            )
        # The token brokered fine but Slack could not be reached to verify it right
        # now; stay connected rather than flapping on a transient upstream error.
        logger.info(
            "slack_status_verify_upstream_error",
            extra={"operation": exc.operation, "status_code": exc.status_code},
        )
        return SlackStatus(
            connected=True,
            state="connected",
            scopes=list(SLACK_READONLY_SCOPES),
            detail="Connected; Slack API token verification was skipped after an upstream error.",
        )
    except httpx.HTTPError:
        logger.info("slack_status_verify_transport_error")
        return SlackStatus(
            connected=True,
            state="connected",
            scopes=list(SLACK_READONLY_SCOPES),
            detail="Connected; Slack API token verification was skipped after a network error.",
        )

    return SlackStatus(
        connected=True,
        state="connected",
        scopes=access.granted_scopes or list(SLACK_READONLY_SCOPES),
    )


def _new_slack_client(token: str, config: ConnectorsConfig) -> SlackClient:
    return SlackClient(
        access_token=token,
        api_base_url=config.slack.api_base_url,
        timeout_seconds=config.slack.request_timeout_seconds,
    )


async def _slack_client(request: Request, config: ConnectorsConfig) -> SlackClient:
    provider = SlackTokenProvider(config.slack)
    try:
        token = await provider.access_token(request)
    except ConnectorNotConnected as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ConnectorReconnectRequired as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ConnectorTokenError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return _new_slack_client(token, config)


_SLACK_CHANNEL_ID_PATTERN = r"[CGD][A-Z0-9]{2,}"
_SLACK_MESSAGE_TS_PATTERN = r"\d{10,}\.\d{3,}"


def _validate_slack_channel_id(channel_id: str) -> None:
    if not re.fullmatch(_SLACK_CHANNEL_ID_PATTERN, channel_id):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "A Slack channel or DM id is required")


def _validate_slack_message_ts(message_ts: str) -> None:
    if not re.fullmatch(_SLACK_MESSAGE_TS_PATTERN, message_ts):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "A Slack message timestamp is required")
