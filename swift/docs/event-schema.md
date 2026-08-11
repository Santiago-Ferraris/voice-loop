# VoiceLoop event schema

The Engine and any UI talk over a Unix-domain socket at
`~/.local/state/voice-loop/engine.sock` (mode 0600). The wire format is **JSONL**
— one JSON object per line. The UI is a subscriber of this contract and nothing
more, which is what lets "works without a UI" and "another OS can write its own"
both be true. `nc -U ~/.local/state/voice-loop/engine.sock` reads the same stream.

The canonical types live in `Sources/VoiceLoopCore/Events.swift`.

## Envelope

Every Engine→subscriber line is:

```json
{"v":1,"type":"<str>","ts":<ms>,"seq":<n>, …payload}
```

- `v` — schema version (currently `1`).
- `type` — the event name (below).
- `ts` — Unix time in milliseconds.
- `seq` — a per-connection monotonic counter (the `hello` greeting is `seq:0`).

Payload fields sit **alongside** the envelope, flattened.

## Engine → subscriber

| type | fields |
|------|--------|
| `hello` | `version`, `modes[]`, `capabilities[]` |
| `state` | `state`: `idle\|listening\|processing\|speaking\|paused`, `mode`: `raw\|smart\|null` |
| `recording_started` | `mode`: `raw\|smart` |
| `interim_transcript` | `text` |
| `final_transcript` | `text`, `normalized` |
| `router_result` | `target{kind,name?,tty?}`, `rewritten`, `actions[]`, `confidence`, `needs_confirmation`, `prompt?` |
| `naming_prompt` | `suggested`, `task_preview` |
| `action_taken` | `op`: `inject\|open_tab\|rename\|focus`, `target{name?,tty?}`, `text?`, `ok`, `detail?` |
| `tts_started` / `tts_finished` | `text` |
| `recording_cancelled` | `reason`: `esc\|no_speech\|not_claude` |
| `error` | `code`: `no_claude_window\|mic_denied\|accessibility_denied\|automation_denied\|stt_failed\|router_failed\|session_gone`, `message`, `hint?` |

## Subscriber → Engine

One JSON object per line, `{"cmd":"…"}`:

- `confirm` (equivalently a short ⌥M tap)
- `cancel`
- `pause` / `resume`
- `quit`
- `get_state`
