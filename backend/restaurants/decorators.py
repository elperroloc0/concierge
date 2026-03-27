from functools import wraps

from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404

from .models import RestaurantMembership


def get_membership(slug, user):
    """Return the active membership for this user + restaurant, or 404."""
    return get_object_or_404(
        RestaurantMembership,
        restaurant__slug=slug,
        user=user,
        is_active=True,
        restaurant__is_active=True,
    )


def portal_view(require_owner=False, require_kb_edit=False):
    """
    Decorator for portal views.

    Sets request.restaurant and request.membership.
    Returns 403 if role requirements aren't met.
    """
    def decorator(view_func):
        @login_required(login_url="portal_login")
        @wraps(view_func)
        def wrapper(request, slug, *args, **kwargs):
            membership = get_membership(slug, request.user)
            request.restaurant = membership.restaurant
            request.membership = membership

            if require_owner and membership.role != "owner":
                return HttpResponseForbidden("Owner access required.")
            if require_kb_edit and membership.role != "owner" and not membership.can_edit_kb:
                return HttpResponseForbidden("You don't have permission to edit the Knowledge Base.")

            return view_func(request, slug, *args, **kwargs)
        return wrapper
    return decorator
