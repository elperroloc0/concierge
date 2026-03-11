import logging
import time

logger = logging.getLogger(__name__)

# Paths logged at DEBUG (not INFO) — they generate noise without business value
_QUIET_PREFIXES = (
    "/static/",
    "/favicon.ico",
    "/api/retell/",   # already logged in detail by each webhook handler
    "/api/twilio/",   # already logged by twilio_sms_status handler
)


class LoggingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        start_time = time.time()
        response = self.get_response(request)
        duration = time.time() - start_time

        path = request.get_full_path()
        user = request.user if request.user.is_authenticated else "Anonymous"
        method = request.method
        status_code = response.status_code

        level = logging.DEBUG if path.startswith(_QUIET_PREFIXES) else logging.INFO
        logger.log(
            level,
            "Request: %s %s | User: %s | Status: %s | Duration: %.4fs",
            method, path, user, status_code, duration,
        )

        return response
