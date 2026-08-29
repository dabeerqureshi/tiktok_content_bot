TikTok Content Bot — content folder
====================================

Drop your video files (any of: .mp4 .mov .m4v .mkv) into this folder. The bot
will scan it every minute, register any new file (deduplicated by SHA-256 content
hash so renames/duplicates are never uploaded twice), and post 3 videos per day
on the schedule defined by UPLOAD_TIMES in ../.env (default 04:00, 12:00, 20:00).

When every video in this folder has been uploaded, the bot emails you asking for
more content. Uploaded files stay in place unless MOVE_UPLOADED=true, in which
case they are moved to `uploaded/` after a successful post.

Tip: keep fewer than a handful of files here at any moment so the bot never runs
silently — the low-stock warning fires when <=2 videos remain.
