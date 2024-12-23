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
    BusinessInfoViewSet
)

from .class_views import (
    ClassList,
    ClassDetail,
    ClassReviews,
    ClassImageList,
    ClassImageDetail,
    ClassOptionList,
    ClassOptionDetail,
    search_classes_by_location
)

from .instructor_views import (
    SecureInstructorViewSet
)

from .student_views import (
    SecureStudentViewSet
)

from .booking_views import (
    BookingStatusViewSet,
    BookingViewSet
)

from .schedule_views import (
    ScheduleViewSet
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
    check_user_role
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

    # Class Views
    'ClassList',
    'ClassDetail',
    'ClassReviews',
    'ClassImageList',
    'ClassImageDetail',
    'ClassOptionList',
    'ClassOptionDetail',
    'search_classes_by_location',

    # Instructor Views
    'SecureInstructorViewSet',

    # Student Views
    'SecureStudentViewSet',

    # Booking Views
    'BookingStatusViewSet',
    'BookingViewSet',

    # Schedule Views
    'ScheduleViewSet',

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

    # Utils
    'haversine_distance',
]