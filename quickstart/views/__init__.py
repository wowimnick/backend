from .auth_views import (
    CustomLoginView,
    CustomTokenObtainPairView,
    CustomTokenRefreshView,
    LogoutView,
    UserUpdateView,
    CustomRegisterView
)

from .business_views import (
    BusinessViewSet,
    BusinessInfoDetail,
    BusinessInfoViewSet,
    register_business,
)

from .class_views import (
    ClassImageList,
    ClassImageDetail,
    ClassOptionDetail,
    ScheduleViewSet,
    ScheduleInstanceViewSet,
    ScheduleBreakViewSet,
    ClassViewSet,
)

from .student_views import (
    StudentProfileViewSet,
)

from .booking_views import (
    BookingViewSet,
)

from .role_views import (
    UserRoleView,
    RoleViewSet
)

from ..utils.permissions import (
    BaseUserDataPermission,
    IsAdminUser,
    IsBusinessOwner,
    IsManager,
    IsInstructor,
    check_user_role,
    check_user_can_create_class,
)

from .revenue_analytics_views import (
    RevenueAnalyticsView,
)

from .review_views import (
    ReviewSubmission,
    ClassReviews,
)

from .utils import (
    haversine_distance,
)

__all__ = [
    # Auth Views
    'CustomLoginView',
    'CustomTokenObtainPairView',
    'CustomTokenRefreshView',
    'LogoutView',
    'UserUpdateView',
    'CustomRegisterView',

    # Business Views
    'BusinessViewSet',
    'BusinessInfoDetail',
    'BusinessInfoViewSet',
    'register_business',

    # Class Views
    'ClassImageList',
    'ClassImageDetail',
    'ClassOptionDetail',
    'ScheduleViewSet',
    'ScheduleInstanceViewSet',
    'ScheduleBreakViewSet',
    'ClassViewSet',

    # Student Views
    'StudentProfileViewSet',

    # Booking Views
    'BookingViewSet',

    # Role Views
    'UserRoleView',
    'RoleViewSet',

    # Permissions
    'BaseUserDataPermission',
    'IsAdminUser',
    'IsBusinessOwner',
    'IsManager',
    'IsInstructor',
    'check_user_role',
    'check_user_can_create_class',

    # Revenue Analytics
    'RevenueAnalyticsView',

    # Review Views
    'ReviewSubmission',
    'ClassReviews',

    # Utils
    'haversine_distance',
]