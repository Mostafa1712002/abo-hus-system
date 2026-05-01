# Linking Shorts back to their parent main video

## Problem

When viewing one of our YouTube Shorts, there was no link/chip pointing
back to the parent main lecture (the "related video" / "from the video"
slot in the YouTube UI was empty). Viewers couldn't easily find the
full lecture from a Short.

## What YouTube actually supports

We researched five candidate methods. Findings as of May 2026:

| Method | API support? | Notes |
|---|---|---|
| `relatedToVideoId` on `videos.insert` | Deprecated | Removed for new uploads. |
| `endScreens` on Video resource / `videos.update` | **No** | Open feature request on Google Issue Tracker since Jan 2025 ([387277988](https://issuetracker.google.com/issues/387277988)). The Video resource has no `endScreens` or `cards` property. |
| `cards.insert` | **No** | Same as above. |
| YouTube Studio "Connect" / Source linking | UI-only | No public API. Requires manual step per Short. |
| Description-based linking | **Yes** | YouTube auto-renders the first URL in a Short's description as a clickable "chip" under the video. Supported across web, mobile, and the Shorts player. |
| `commentThreads.insert` from channel owner | **Yes** | Channel-owner comments get a verified badge and float to the top of the comments tab. (Programmatic *pinning* is still not exposed.) |

## Implemented approach

We implement **both** description linking and an owner-comment for
maximum visibility:

1. **Description**: the very first line of every Short upload is now
   `شاهد المحاضرة كاملة: https://youtu.be/<MAIN_ID>`. The first URL
   in a Short's description is what YouTube uses to render the
   "from the video" chip.
2. **Comment**: after upload, the pipeline calls
   `commentThreads.insert` from the channel owner with the same URL.
   The comment shows up with the owner badge and is highly visible in
   the comment tab — even though it can't be programmatically pinned.

## Code

- `src/youtube_uploader.py`: new `link_short_to_main(youtube, short_id, main_id)`
  function that updates the description (idempotent — bails if the URL
  is already in the first 200 chars) and posts the owner comment.
- `src/pipeline.py::_upload_shorts_for_video`: the description body
  is reordered so the parent URL is the very first line, and after
  every successful `upload_short` we call `link_short_to_main`.
- `fix_shorts_link_parent.py` (project root): backfill script for
  Shorts already on YouTube. Reads the SQLite tracker (`videos.metadata_json.short_video_ids`)
  and links each Short to its parent.

## Backfill

Done on May 1, 2026 for the 9 Shorts that had been uploaded before
this feature shipped:

- 3 Shorts under `R3b57poudYE` (أسرار الرسالة)
- 3 Shorts under `t-UEoM8O18k` (السنة خصصت عموم القرآن)
- 3 Shorts under `bSecVyyTBHc` (حقيقة الإسلام ودين الأنبياء)

All 9 had their description updated and an owner comment posted with
the parent URL. Verified by re-fetching the snippet from
`videos.list` and listing `commentThreads`.

## Caveats

- We can't programmatically pin the auto-comment — that still needs
  Studio. The owner badge already gives it ranking priority though.
- We can't add an end-screen pointing at the main video — Google has
  not exposed this surface.
- The description-chip rendering ultimately depends on YouTube's
  client. It's been stable for years but isn't formally documented.
- Quota cost per Short: `videos.list` (1) + `videos.update` (50) +
  `commentThreads.insert` (50) ≈ 101 units. With the existing 10k/day
  quota and ~3 shorts per upload, this is negligible.
