from django.urls import path, include
from .views import (
    BookingStatusViewSet, BookingViewSet, BusinessInfoDetail, BusinessInfoViewSet, ClassList, ClassDetail, ClassReviews,
    CustomLoginView, CustomTokenObtainPairView, CustomTokenRefreshView, LogoutView, ScheduleViewSet, SecureStudentViewSet, UserRoleView, UserUpdateView, get_google_maps_api_key,
    ClassImageList, ClassImageDetail, SubClassesViewSet, SubClassDetail, CustomRegisterView,
    SecureInstructorViewSet, RoleViewSet, search_classes_by_location
)
from allauth.account.views import confirm_email
from rest_framework_simplejwt.views import TokenRefreshView
from django.views.generic import TemplateView

urlpatterns = [
    path('auth/', include('dj_rest_auth.urls')),
    path('login/', CustomTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('user/role/', UserRoleView.as_view(), name='user-role'),
    path('token/refresh/', CustomTokenRefreshView.as_view(), name='token_refresh'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('user/update/', UserUpdateView.as_view(), name='user-update'),
    path('token/', CustomTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('auth/registration/', CustomRegisterView.as_view(), name='rest_register'),
    path('account-confirm-email/', TemplateView.as_view(template_name="email_confirmation.html"), name='account_email_verification_sent'),
    path('account-confirm-email/<str:key>/', confirm_email, name='account_confirm_email'),
    path('classes/', ClassList.as_view(), name='class-list'),
    path('classes/search/', search_classes_by_location, name='class-search'),
    path('classes/<int:pk>/', ClassDetail.as_view(), name='class-detail'),
    path('classes/<int:pk>/reviews/', ClassReviews.as_view(), name='class-reviews'),
    path('classes/<int:pk>/subclasses/', SubClassesViewSet.as_view(), name='class-subclasses'),
    path('classes/<int:pk>/subclasses/<int:subclass_id>/', SubClassDetail.as_view(), name='class-subclass-detail'),
    path('classes/<int:pk>/images/', ClassImageList.as_view(), name='class-images'),
    path('classes/images/<int:pk>/', ClassImageDetail.as_view(), name='class-image-detail'),
    path('businesses/', BusinessInfoViewSet.as_view({'get': 'list', 'post': 'create'}), name='business-list'),
    path('businesses/<int:pk>/', BusinessInfoDetail.as_view(), name='business-detail'),

    # New URLs for instructors and roles
    path('instructors/', SecureInstructorViewSet.as_view({'get': 'list', 'post': 'create'}), name='instructor-list'),
    path('instructors/<int:pk>/', SecureInstructorViewSet.as_view({'get': 'retrieve', 'put': 'update', 'patch': 'partial_update', 'delete': 'destroy'}), name='instructor-detail'),
    path('instructors/<int:pk>/education/', SecureInstructorViewSet.as_view({'post': 'add_education'}), name='instructor-add-education'),
    path('instructors/<int:pk>/certification/', SecureInstructorViewSet.as_view({'post': 'add_certification'}), name='instructor-add-certification'),
    path('instructors/<int:pk>/skill/', SecureInstructorViewSet.as_view({'post': 'add_skill'}), name='instructor-add-skill'),
    path('instructors/<int:pk>/note/', SecureInstructorViewSet.as_view({'post': 'add_note'}), name='instructor-add-note'),
    path('roles/', RoleViewSet.as_view({'get': 'list'}), name='role-list'),
    path('roles/<int:pk>/', RoleViewSet.as_view({'get': 'retrieve'}), name='role-detail'),

    # Student URLs
    path('students/', SecureStudentViewSet.as_view({'get': 'list', 'post': 'create'}), name='student-list'),
    path('students/<int:pk>/', SecureStudentViewSet.as_view({'get': 'retrieve', 'put': 'update', 'patch': 'partial_update', 'delete': 'destroy'}), name='student-detail'),
    path('students/<int:pk>/note/', SecureStudentViewSet.as_view({'post': 'add_note'}), name='student-add-note'),
    path('students/<int:pk>/enroll/', SecureStudentViewSet.as_view({'post': 'enroll'}), name='student-enroll'),
    path('students/<int:pk>/attendance/', SecureStudentViewSet.as_view({'post': 'record_attendance'}), name='student-record-attendance'),
    path('students/<int:pk>/performance/', SecureStudentViewSet.as_view({'post': 'record_performance'}), name='student-record-performance'),
    path('students/<int:pk>/bookings/', SecureStudentViewSet.as_view({'get': 'bookings'}), name='student-bookings'),
    path('students/<int:pk>/create_booking/', SecureStudentViewSet.as_view({'post': 'create_booking'}), name='student-create-booking'),

    # Booking URLs
    path('bookings/', BookingViewSet.as_view({'get': 'list', 'post': 'create'}), name='booking-list'),
    path('bookings/<int:pk>/', BookingViewSet.as_view({'get': 'retrieve', 'put': 'update', 'patch': 'partial_update', 'delete': 'destroy'}), name='booking-detail'),

    # Booking Status URLs
    path('booking-statuses/', BookingStatusViewSet.as_view({'get': 'list'}), name='booking-status-list'),
    path('booking-statuses/<int:pk>/', BookingStatusViewSet.as_view({'get': 'retrieve'}), name='booking-status-detail'),
    
    # Schedule List Operations

    path('schedules/', 
         ScheduleViewSet.as_view({
             'get': 'list',
             'post': 'create'
         }), 
         name='schedule-list'),
    
    # Schedule Detail Operations
    path('schedules/<int:pk>/', 
         ScheduleViewSet.as_view({
             'get': 'retrieve',
             'put': 'update',
             'patch': 'partial_update',
             'delete': 'destroy'
         }), 
         name='schedule-detail'),
    
    # Schedule Student Operations
    path('schedules/<int:pk>/add_student/',
         ScheduleViewSet.as_view({'post': 'add_student'}),
         name='schedule-add-student'),
    
    path('schedules/<int:pk>/remove_student/',
         ScheduleViewSet.as_view({'post': 'remove_student'}),
         name='schedule-remove-student'),
    
    # Attendance Management
    path('schedules/<int:pk>/mark_attendance/',
         ScheduleViewSet.as_view({'post': 'mark_attendance'}),
         name='schedule-mark-attendance'),
    
    # Bulk Operations
    path('schedules/bulk_create/',
         ScheduleViewSet.as_view({'post': 'bulk_create'}),
         name='schedule-bulk-create'),
    
    path('schedules/bulk_update/',
         ScheduleViewSet.as_view({'post': 'bulk_update'}),
         name='schedule-bulk-update'),
    
    # Schedule Filtering
    path('schedules/date/<str:date>/',
         ScheduleViewSet.as_view({'get': 'by_date'}),
         name='schedule-by-date'),
    
    path('schedules/instructor/<int:instructor_id>/',
         ScheduleViewSet.as_view({'get': 'by_instructor'}),
         name='schedule-by-instructor'),
    
    path('schedules/room/<str:room>/',
         ScheduleViewSet.as_view({'get': 'by_room'}),
         name='schedule-by-room'),
]