"""
Download a YouTube video using yt-dlp.

Requirements:
    pip install yt-dlp
"""

from pathlib import Path

import yt_dlp


def download_youtube_video(youtube_url: str, output_directory: str = "downloads") -> str:
    """
    Download a YouTube video.

    Args:
        youtube_url: Full YouTube video URL.
        output_directory: Directory where the video will be saved.

    Returns:
        Path to the downloaded file.
    """
    output_path = Path(output_directory)
    output_path.mkdir(parents=True, exist_ok=True)

    ydl_opts = {
        "outtmpl": str(output_path / "%(title)s.%(ext)s"),
        "format": "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(youtube_url, download=True)
        downloaded_file = ydl.prepare_filename(info)

    return downloaded_file


if __name__ == "__main__":
    youtube_link = input("Enter YouTube URL: ").strip()

    try:
        file_path = download_youtube_video(youtube_link)
        print(f"Video downloaded successfully: {file_path}")
    except Exception as exc:
        print(f"Download failed: {exc}")
