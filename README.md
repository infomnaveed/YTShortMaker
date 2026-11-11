# Shorts Maker

Streamlit app: long video -> ~1 minute vertical (9:16) clips with word-highlighted English captions.
Uses faster-whisper (speech to text), FFmpeg (cut, crop, burn captions) and yt-dlp (optional YouTube download).

## Run locally
    sudo apt install ffmpeg fonts-dejavu-core   # or: brew install ffmpeg
    pip install -r requirements.txt
    streamlit run app.py

## Deploy on Streamlit Community Cloud
1. Push this folder to a GitHub repo (keep `.streamlit/` and `packages.txt`).
2. Go to share.streamlit.io, click "Create app", pick the repo, branch `main`, main file `app.py`.
3. Under Advanced settings choose Python 3.11 or 3.12, then Deploy.
