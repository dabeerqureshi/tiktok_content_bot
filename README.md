# tiktok_content_bot
Python Application
│
├── SQLite
│   └── videos / clips / queue / posts / errors
│
├── YouTube
│   └── source video discovery + download
│
├── FFmpeg
│   └── video/audio processing
│
├── faster-whisper
│   └── transcription
│
├── Ollama
│   └── clip analysis + titles + captions
│
├── TikTok API
│   └── publishing
│
├── Scheduler
│   └── 3 posts/day + queue maintenance
│
└── Email
    └── errors + status alerts


tiktok_content_bot/
│
├── app.py
├── config.py
│
├── database/
│   ├── db.py
│   ├── models.py
│   └── migrations.py
│
├── services/
│   ├── youtube.py
│   ├── downloader.py
│   ├── ffmpeg.py
│   ├── transcription.py
│   ├── ollama.py
│   ├── tiktok.py
│   ├── email.py
│   └── scheduler.py
│
├── workers/
│   ├── video_worker.py
│   ├── clip_worker.py
│   └── publish_worker.py
│
├── storage/
│   ├── originals/
│   ├── clips/
│   └── failed/
│
├── logs/
│   └── app.log
│
├── data/
│   └── content.db
│
├── .env
├── requirements.txt
└── README.md
