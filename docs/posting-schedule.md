# Posting Schedule (Cairo TZ)

Optimized for Arabic Islamic content audience peaks.

## Publish Slots

| Series | Publish | Why |
|---|---|---|
| شرح الرسالة | 06:00 | After Fajr — peak time for morning lecture audience |
| العبودية | 20:00 | Evening prime time on YouTube/FB/IG |

## Cron (`/etc/cron` on VPS, `CRON_TZ=Africa/Cairo`)

| Time | Job | Lead time |
|---|---|---|
| 03:00 | `src.cleanup` | — |
| 04:00 | `upload-batch --series 'شرح الرسالة' --limit 1` | 2h before publish |
| 18:00 | `upload-batch --series 'العبودية' --limit 1` | 2h before publish |
| every 15 min | `main.py process` | picks up scheduled work |

The cron triggers an **upload**; the pipeline reads `youtube.publish_times_local` from `config.json` and assigns the next available slot via `get_next_publish_time()`.

## Config

`config.json → youtube.publish_times_local`:
```json
["06:00", "20:00"]
```

## Changed from

- 09:00 / 09:05 cron → 11:00 / 18:00 publish (medium engagement window)
- → 04:00 / 18:00 cron → 06:00 / 20:00 publish (peak engagement)
