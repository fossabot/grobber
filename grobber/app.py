import logging
import os

import sentry_sdk
from quart import Quart, Response

from . import __info__, sources
from .blueprints import *
from .exceptions import GrobberException
from .models import UIDConverter
from .utils import *

log = logging.getLogger(__name__)

app = Quart("grobber", static_url_path="/")

app.url_map.converters["UID"] = UIDConverter

app.register_blueprint(anime_blueprint)
app.register_blueprint(debug_blueprint)

host_url = os.getenv("HOST_URL")
if host_url:
    app.config["HOST_URL"] = add_http_scheme(host_url)

sentry_sdk.init(release=f"grobber@{__info__.__version__}")
log.info(f"grobber version {__info__.__version__} running!")


@app.errorhandler(GrobberException)
def handle_grobber_exception(exc: GrobberException) -> Response:
    return error_response(exc)


@app.teardown_appcontext
def teardown_app_context(*_):
    do_later(sources.save_dirty())


@app.after_request
async def after_request(response: Response) -> Response:
    response.headers["Grobber-Version"] = __info__.__version__
    return response


@app.route("/dolos-info")
async def get_dolos_info() -> Response:
    return create_response(id="grobber", version=__info__.__version__)
