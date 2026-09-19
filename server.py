from app.config import (
    MEDIAMTX_HOST,
    MEDIAMTX_HLS_PORT,
    MEDIAMTX_RTSP_PORT,
    PORT,
    VERSION,
)
from app.http_server import make_server

if __name__ == "__main__":
    server = make_server()
    print(f"Listening on 0.0.0.0:{PORT} {VERSION}")
    print(f"MediaMTX RTSP: {MEDIAMTX_HOST}:{MEDIAMTX_RTSP_PORT}")
    print(f"MediaMTX HLS:  {MEDIAMTX_HOST}:{MEDIAMTX_HLS_PORT}")
    server.serve_forever()
