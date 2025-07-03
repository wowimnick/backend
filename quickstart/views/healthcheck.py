from django.http import HttpResponse


def health_check(request):
    """A simple view for the Load Balancer health check."""
    return HttpResponse("OK")
