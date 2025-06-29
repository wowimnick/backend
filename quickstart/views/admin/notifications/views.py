from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone
from quickstart.models import CustomUser
from .utils import verify_unsubscribe_token
import logging

logger = logging.getLogger(__name__)


def unsubscribe_user(request, token):
    """
    Handles the user unsubscribe action from an email link.
    """
    user_id = verify_unsubscribe_token(token)

    if not user_id:
        logger.warning(f"Invalid or expired unsubscribe token received: {token}")
        return render(
            request,
            "notifications/unsubscribe_status.html",
            {
                "success": False,
                "message": "This unsubscribe link is invalid or has expired.",
            },
        )

    try:
        user = CustomUser.objects.get(pk=user_id)
        if not user.is_unsubscribed:
            user.is_unsubscribed = True
            user.unsubscribed_at = timezone.now()
            user.save(update_fields=["is_unsubscribed", "unsubscribed_at"])
            logger.info(
                f"User {user.email} (ID: {user.pk}) has successfully unsubscribed."
            )

        return render(
            request,
            "notifications/unsubscribe_status.html",
            {
                "success": True,
                "message": f"You have been successfully unsubscribed from marketing emails. Your email is {user.email}.",
            },
        )

    except CustomUser.DoesNotExist:
        logger.error(f"User with ID {user_id} from valid token not found in database.")
        return render(
            request,
            "notifications/unsubscribe_status.html",
            {
                "success": False,
                "message": "Could not find an account associated with this link.",
            },
        )
    except Exception as e:
        logger.error(
            f"An unexpected error occurred during unsubscribe for token {token}: {e}"
        )
        return HttpResponse(
            "An unexpected error occurred. Please contact support.", status=500
        )
