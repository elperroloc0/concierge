from decimal import Decimal


def balance_status(request):
    """Add balance_status and agent_status to all portal template contexts."""
    if not request.user.is_authenticated:
        return {"balance_status": "", "agent_status": "inactive"}
    try:
        sub = request.user.restaurant.subscription
        bal = sub.communication_balance

        # Balance status for sidebar dot
        if bal <= Decimal("3"):
            bal_status = "critical"
        elif bal <= Decimal("8"):
            bal_status = "warning"
        else:
            bal_status = ""

        # Agent status for topbar badge
        if sub.status in ("active", "trialing"):
            if bal <= Decimal("0"):
                agent_status = "no_credits"
            else:
                agent_status = "active"
        elif sub.status == "past_due":
            agent_status = "past_due"
        else:
            agent_status = "inactive"

        return {"balance_status": bal_status, "agent_status": agent_status}
    except Exception:
        pass
    return {"balance_status": "", "agent_status": "inactive"}
