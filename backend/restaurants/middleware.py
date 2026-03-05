import logging
import time

logger = logging.getLogger(__name__)

class LoggingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Start time of the request
        start_time = time.time()

        # Get the response
        response = self.get_response(request)

        # Calculate execution time
        duration = time.time() - start_time

        # Log request details
        user = request.user if request.user.is_authenticated else "Anonymous"
        path = request.get_full_path()
        method = request.method
        status_code = response.status_code

        logger.info(
            f"Request: {method} {path} | User: {user} | Status: {status_code} | Duration: {duration:.4f}s"
        )

        return response
