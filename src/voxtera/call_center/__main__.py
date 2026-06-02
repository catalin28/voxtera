"""Allow running as: python -m voxtera.call_center"""

from voxtera.call_center.server import create_app

if __name__ == "__main__":
    import os

    from aiohttp import web
    from dotenv import load_dotenv
    from loguru import logger

    load_dotenv()
    port = int(os.environ.get("CALL_CENTER_PORT", "8100"))
    logger.info("Starting Call Center admin server on http://localhost:{}", port)
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=port)
