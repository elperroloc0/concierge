import markdown as _md
from django import template
from django.utils.safestring import mark_safe

register = template.Library()


@register.filter(is_safe=True)
def render_markdown(value):
    """Convert markdown text to safe HTML."""
    if not value:
        return ""
    html = _md.markdown(
        value,
        extensions=["nl2br", "tables"],
    )
    return mark_safe(html)
