# Module time helpers

Host and sandbox modules share the same pure `ctx.tools` implementation, without capabilities or a client. Existing helpers and proxies remain available.

- `ctx.tools.duration(seconds: int | float, language: str = "en") -> str`
- `ctx.tools.timestamp(value: int | float, language: str = "en") -> str`
- `ctx.tools.time_range(start: int | float, end: int | float | None = None, language: str = "en") -> str`
- `ctx.tools.uptime_fmt(language: str = "en") -> str` delegates to `duration(ctx.tools.uptime(), language)`. This legacy clock measures toolkit age; `.ping` instead passes the current Runtime's monotonic age to `duration`, so reloading a module does not reset userbot uptime.

`duration` decomposes whole seconds, largest unit first, omitting every zero component. Subsecond and negative inputs render “less than a second” (localized), never zero fields. NaN/infinity are rejected by integer conversion. Pass integer seconds to preserve exact values on geological scales. Languages: `en`, `ru`, `uk`, `kz` (`kk` alias), `ja`; locale suffixes are accepted and unknown languages fall back to English. Russian/Ukrainian quantities use grammatical plural forms.

Fixed duration approximations, **not calendar arithmetic**: year = 365.25 days = 31,557,600 seconds; month = year / 12 = 30.4375 days. Week = 7 days. Century = 100 years; millennium = 1,000 years. The custom display scale defines epoch = 1,000,000 years, era = 100,000,000 years and eon = 1,000,000,000 years; these are **not actual fixed geological intervals**.

`timestamp` accepts Unix seconds and returns a localized date and time explicitly in UTC. `time_range` collapses equal displayed seconds and repeats the date only across different dates. Both return text, not HTML; escape it when embedding in HTML.

```python
ctx.tools.duration(3661, "ru")
# 1 час 1 минута 1 секунда
ctx.tools.timestamp(0, "ru")
# 1 января 1970 · 00:00:00 UTC
```

## Ping template

Open `.conf ping` and edit `template` using the existing module configuration UI (use your configured command prefix). An empty or whitespace-only string selects the localized default. Maximum template length: 3,000 characters.

```html
<b>Latency</b> <code>{latency} ms</code>
<b>Uptime</b> <code>{uptime}</code>
```

`{latency}` is the measured response/edit round trip in milliseconds, with three decimal places; `{uptime}` is the escaped localized userbot duration. Replacement uses `ctx.tools.render`: unknown placeholders, format specifications and unmatched braces are literal, not evaluated. Use valid ordinary Telegram HTML, not Rich-only tags. No Rich mode is enabled by ping.

Logs use the same timestamp helper, one shared timestamp per anchored three-second group, a native summary table, quotes and expandable multiline preformatted details. Presentation adds no emoji; original diagnostic payloads are not stripped or rewritten.
