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
    ClassReviews,
    ClassImageList,
    ClassImageDetail,
    ClassOptionDetail,
    ScheduleViewSet,
    toggle_option_active,
    ScheduleInstanceViewSet,
    ScheduleBreakViewSet,
    ClassViewSet,
)

from .instructor_views import (
    InstructorViewSet,
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

from .permissions import (
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
    'ClassReviews',
    'ClassImageList',
    'ClassImageDetail',
    'ClassOptionDetail',
    'ScheduleViewSet',
    'toggle_option_active',
    'ScheduleInstanceViewSet',
    'ScheduleBreakViewSet',
    'ClassViewSet',

    # Instructor Views
    'InstructorViewSet',

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

    # Utils
    'haversine_distance',
]