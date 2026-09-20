from __future__ import annotations

from typing import Any, TypedDict, cast


class TL(TypedDict, total=False):
    _: str
    id: int
    flags: int
    date: int
    message: str
    entities: list[Any]
    reply_markup: dict[str, Any]
    media: dict[str, Any]
    peer: Any
    peer_id: Any
    from_id: Any
    user_id: int
    chat_id: int
    channel_id: int
    access_hash: int
    username: str
    phone: str
    first_name: str
    last_name: str
    title: str
    premium: bool
    out: bool
    mentioned: bool
    silent: bool
    post: bool
    from_scheduled: bool
    edit_date: int
    grouped_id: int
    ttl_period: int
    reply_to: dict[str, Any]
    replies: dict[str, Any]
    views: int
    forwards: int
    reactions: dict[str, Any]
    restriction_reason: list[Any]
    via_bot_id: int
    via_business_bot_id: int
    offline: bool
    bot: bool
    verified: bool
    restricted: bool
    min: bool
    fake: bool
    scam: bool
    attach_menu_enabled: bool
    bot_attach_menu: bool
    premium_required: bool
    send_paid_messages_stars: int
    color: dict[str, Any]
    profile_color: dict[str, Any]
    emoji_status: dict[str, Any]
    usernames: list[Any]
    stories_max_id: int
    bot_info: dict[str, Any]
    photo: dict[str, Any]
    status: dict[str, Any]
    users: list[Any]
    chats: list[Any]
    updates: list[Any]
    seq: int
    pts: int
    pts_count: int
    qts: int
    unread_count: int
    result: Any
    random_id: int
    query_id: int
    offset: str
    query: str
    geo: dict[str, Any]
    peer_type: dict[str, Any]
    results: list[Any]
    switch_pm: dict[str, Any]
    switch_webview: dict[str, Any]
    cache_time: int
    gallery: bool
    private: bool
    next_offset: str
    data: bytes | str
    msg_id: int
    chat_instance: int
    game_short_name: str
    inline_message_id: str
    dc_id: int
    owner_id: int
    file_reference: bytes
    parts: int
    name: str
    md5_checksum: str
    mime_type: str
    size: int
    thumbs: list[Any]
    video_thumbs: list[Any]
    attributes: list[Any]
    nosound_video: bool
    force_file: bool
    spoiler: bool
    ttl_seconds: int
    sticker_set: dict[str, Any]
    mask: bool
    file: dict[str, Any]
    thumb: dict[str, Any]
    no_webpage: bool
    invert_media: bool
    reply_to_msg_id: int
    top_msg_id: int
    reply_to_peer_id: Any
    quote_text: str
    quote_entities: list[Any]
    quote_offset: int
    forum_topic: bool
    folder_id: int
    notify_settings: dict[str, Any]
    mute_until: int
    show_previews: bool
    silent_flag: bool
    mute_stories: bool
    stories_hide_sender: bool
    stories_and_max_id: int
    pinned: bool
    unread_mark: bool
    view_forum_as_messages: bool
    draft: dict[str, Any]
    unread_mentions_count: int
    unread_reactions_count: int
    ttl_period_days: int
    theme_emoticon: str
    wall_paper: dict[str, Any]
    wallpaper_overridden: bool
    translated_text: dict[str, Any]
    factcheck: dict[str, Any]
    reported: bool
    reactions_are_possible: bool
    invert_media_flag: bool
    offline_flag: bool
    bot_nochats: bool
    bot_inline_geo: bool
    bot_attach_menu_enabled: bool
    attach_menu_enabled_flag: bool
    bot_can_edit: bool
    close_friend: bool
    stories_hidden: bool
    stories_unavailable: bool
    contact_require_premium: bool
    bot_forum_view: bool
    bot_forum_can_manage: bool
    bot_business: bool
    bot_has_main_app: bool
    bot_has_preview_medias: bool
    bot_can_manage_emoji_status: bool
    place: dict[str, Any]
    geo_point: dict[str, Any]
    address: str
    provider: str
    venue_id: str
    venue_type: str
    period: int
    proximity_notification_radius: int
    heading: int
    lat: float
    long: float
    accuracy_radius: int
    url: str
    webpage: dict[str, Any]
    caption: str
    document: dict[str, Any]
    photo_id: int
    video: bool
    round_message: bool
    supports_streaming: bool
    nosound: bool
    preload_prefix_size: int
    alt_document: dict[str, Any]
    video_cover: dict[str, Any]
    video_timestamp: int
    stickerset: dict[str, Any]
    mask_coords: dict[str, Any]
    emoticon: str
    duration: int
    w: int
    h: int
    vc_id: int
    call: dict[str, Any]
    reason: dict[str, Any]
    receive_date: int
    bytes: bytes
    file_id: int
    location: dict[str, Any]
    thumb_size: str
    precise: bool
    big: bool
    migrated_to: dict[str, Any]
    migrated_from: dict[str, Any]
    participants_count: int
    admins_count: int
    kicked_count: int
    banned_count: int
    online_count: int
    exported_invite: dict[str, Any]
    stickers_count: int
    gigagroup: bool
    forum: bool
    join_to_send: bool
    join_request: bool
    forum_tabs: bool
    stories_hidden_group: bool
    stories_hidden_min: bool
    broadcast: bool
    megagroup: bool
    signatures: bool
    has_link: bool
    has_geo: bool
    slowmode_enabled: bool
    call_active: bool
    call_not_empty: bool
    fake_flag: bool
    gigagroup_flag: bool
    noforwards: bool
    join_request_flag: bool
    forum_flag: bool
    stories_hidden_flag: bool
    stories_hidden_min_flag: bool
    signature_profiles: bool
    autotranslation: bool
    broadcast_messages_allowed: bool
    monoforum: bool
    forum_tabs_flag: bool
    level: int
    restriction_reason_list: list[Any]
    default_banned_rights: dict[str, Any]
    banned_rights: dict[str, Any]
    admin_rights: dict[str, Any]
    deactivated: bool
    left: bool
    creator: bool
    participants: dict[str, Any]
    chat: dict[str, Any]
    user: dict[str, Any]
    channel: dict[str, Any]
    dialogs: list[Any]
    messages: list[Any]
    count: int
    new_messages: list[Any]
    other_updates: list[Any]
    state: dict[str, Any]
    intermediate_state: dict[str, Any]
    pts_total_limit: int
    timeout: int
    folder: dict[str, Any]
    filter: dict[str, Any]
    emoticon_flag: str
    top_message: int
    read_inbox_max_id: int
    read_outbox_max_id: int
    unread_count_dialog: int
    unread_mentions_count_dialog: int
    unread_reactions_count_dialog: int
    notify_settings_dialog: dict[str, Any]
    pts_dialog: int
    folder_id_dialog: int
    ttl_period_dialog: int
    theme_emoticon_dialog: str
    is_forum: bool
    view_forum_as_messages_dialog: bool
    draft_dialog: dict[str, Any]
    peer_dialog: Any
    top_message_forum: int
    read_inbox_max_id_forum: int
    read_outbox_max_id_forum: int
    unread_count_forum: int
    unread_mentions_count_forum: int
    unread_reactions_count_forum: int
    title_forum: str
    icon_emoji_id: int
    icon_color: int
    closed: bool
    hidden: bool
    id_forum: int
    date_forum: int
    top_message_id: int
    read_inbox_max_id_topic: int
    read_outbox_max_id_topic: int
    unread_count_topic: int
    unread_mentions_count_topic: int
    unread_reactions_count_topic: int
    from_id_topic: Any
    notify_settings_topic: dict[str, Any]
    draft_topic: dict[str, Any]
    nopaid_messages_exception: bool
    send_paid_messages_stars_topic: int
    min_id: int
    max_id: int
    offset_id: int
    offset_date: int
    add_offset: int
    limit: int
    hash: int
    exclude_pinned: bool
    folder_id_filter: int
    q: str
    filter_search: dict[str, Any]
    min_date: int
    max_date: int
    offset_rate: int
    offset_peer: Any
    saved_peer_id: Any
    saved_reaction: list[Any]
    broadcasts_only: bool
    groups_only: bool
    users_only: bool


class Button(TypedDict, total=False):
    text: str
    callback_data: str
    url: str
    copy_text: str
    icon_custom_emoji_id: str
    style: str
    callback: Any
    handler: Any
    payload: Any
    input: str
    switch_inline_query: str
    switch_inline_query_current_chat: str
    web_app: dict[str, Any]
    login_url: dict[str, Any]
    user_id: int
    callback_game: dict[str, Any]


class ReplyMarkup(TypedDict, total=False):
    _: str
    inline_keyboard: list[list[Button]]
    keyboard: list[list[Button]]
    rows: list[Any]
    flags: int
    selective: bool
    single_use: bool
    persistent: bool
    resize: bool
    placeholder: str
    is_personal: bool


class MessageEntity(TypedDict, total=False):
    _: str
    offset: int
    length: int
    url: str
    language: str
    user_id: int
    document_id: int
    collapsed: bool


class InputPeer(TypedDict, total=False):
    _: str
    user_id: int
    chat_id: int
    channel_id: int
    access_hash: int


class InputFile(TypedDict, total=False):
    _: str
    id: int
    parts: int
    name: str
    md5_checksum: str


class InputMedia(TypedDict, total=False):
    _: str
    file: dict[str, Any]
    mime_type: str
    attributes: list[Any]
    force_file: bool
    spoiler: bool
    ttl_seconds: int
    stickerset: dict[str, Any]
    nosound_video: bool
    video: bool
    round_message: bool
    supports_streaming: bool
    thumb: dict[str, Any]
    photo: dict[str, Any]
    geo_point: dict[str, Any]
    period: int
    heading: int
    proximity_notification_radius: int
    url: str
    webpage: dict[str, Any]


def as_tl(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def as_buttons(value: object) -> list[list[Button]]:
    if not isinstance(value, list) or not value:
        return []
    if isinstance(value[0], list):
        return cast(list[list[Button]], value)
    return [cast(list[Button], value)]
