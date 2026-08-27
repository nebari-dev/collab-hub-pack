import uvicorn

from collab_hub_api.config import Config
from collab_hub_api.core import make_app
from collab_hub_api.observability import configure_logging

if __name__ == "__main__":
    config = Config.parse()

    configure_logging(config.observability.logging)

    app = make_app(config)

    uvicorn.run(
        app,
        host=config.server.hostname,
        port=config.server.port,
        proxy_headers=config.server.proxy_headers,
        forwarded_allow_ips=config.server.forwarded_allow_ips,
        log_config=None,
        use_colors=False,
    )
