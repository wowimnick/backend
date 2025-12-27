import pytest
import logging
from django.urls import reverse, NoReverseMatch
from rest_framework.test import APIClient
from rest_framework import status
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.contrib.auth import get_user_model

# Import Models
from quickstart.models import (
    ClassesMain, 
    Booking, 
    Payout, 
    Discount, 
    AuditLog, 
    VerificationRequest,
    BusinessInfo
)

# Import Routers for Automatic Testing
from quickstart.urls import (
    admin_router, 
    business_management_router, 
    user_self_router
)

# Import Factories
from .factories import (
    UserFactory, 
    RoleFactory, 
    BusinessFactory, 
    ClassFactory,
    PermissionFactory,
    ClassOptionFactory,
    ScheduleFactory,
    ScheduleInstanceFactory,
    BookingFactory,
    PayoutFactory,
    DiscountFactory,
    AuditLogFactory,
    VerificationRequestFactory
)

User = get_user_model()
logger = logging.getLogger(__name__)

@pytest.mark.django_db
class TestEndpointSecurity:
    """
    Specific Security Logic Tests:
    Verifies that RBAC, IDOR protection, and Hierarchy rules function correctly.
    """
    
    def setup_method(self):
        self.client = APIClient()

        # 1. Define Content Types
        self.ct_classes = ContentType.objects.get_for_model(ClassesMain)
        self.ct_users = ContentType.objects.get_for_model(User)
        self.ct_payouts = ContentType.objects.get_for_model(Payout)
        
        # 2. Create Permissions
        self.perm_access_class_admin = PermissionFactory(codename='access_class_admin', content_type=self.ct_classes)
        self.perm_view_class = PermissionFactory(codename='view_classesmain', content_type=self.ct_classes)
        self.perm_manage_own = PermissionFactory(codename='manage_own_classes', content_type=self.ct_classes)
        
        self.perm_access_user_admin = PermissionFactory(codename='access_user_admin', content_type=self.ct_users)
        self.perm_delete_user = PermissionFactory(codename='delete_customuser', content_type=self.ct_users)
        
        self.perm_access_payout = PermissionFactory(codename='access_payout_admin', content_type=self.ct_payouts)

        # 3. Create Roles
        self.role_student = RoleFactory(name="Student", hierarchy_level=1)
        
        self.role_admin = RoleFactory(
            name="Super Admin", 
            hierarchy_level=100, 
            permissions=[
                self.perm_access_class_admin, 
                self.perm_view_class,
            ]
        )
        
        self.role_business = RoleFactory(
            name="Business Owner",
            hierarchy_level=10,
            permissions=[
                self.perm_manage_own,
                self.perm_access_payout 
            ]
        )

    # ============================================================================
    # 1. VERTICAL ESCALATION (Role Checks)
    # ============================================================================
    
    def test_student_cannot_access_admin_class_list(self):
        logger.info("TEST: Vertical Escalation - Student accessing Admin Classes")
        student = UserFactory(role=self.role_student)
        self.client.force_authenticate(user=student)
        url = reverse('admin-classes-list') 
        response = self.client.get(url)
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_admin_can_access_admin_class_list(self):
        logger.info("TEST: Admin Access - Admin accessing Admin Classes")
        admin = UserFactory(role=self.role_admin)
        self.client.force_authenticate(user=admin)
        url = reverse('admin-classes-list')
        response = self.client.get(url)
        assert response.status_code == status.HTTP_200_OK

    # ============================================================================
    # 2. HORIZONTAL ESCALATION (IDOR)
    # ============================================================================

    def test_business_owner_cannot_edit_others_class(self):
        logger.info("TEST: IDOR - Owner A editing Owner B's class")
        owner_a = UserFactory(role=self.role_business)
        
        owner_b = UserFactory(role=self.role_business)
        biz_b = BusinessFactory(owner=owner_b)
        class_b = ClassFactory(businessId=biz_b, title="Owner B Class")

        self.client.force_authenticate(user=owner_a)
        url = reverse('business-class-detail', kwargs={'pk': class_b.classId})
        
        response = self.client.patch(url, {"title": "Hacked"})
        assert response.status_code in [status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND]

    def test_business_cannot_view_others_payouts(self):
        logger.info("TEST: Financial Privacy - Owner A viewing Owner B's payouts")
        owner_a = UserFactory(role=self.role_business)
        
        owner_b = UserFactory(role=self.role_business)
        biz_b = BusinessFactory(owner=owner_b)
        payout_b = PayoutFactory(business=biz_b)
        
        self.client.force_authenticate(user=owner_a)
        url = reverse('business-payout-detail', kwargs={'pk': payout_b.pk})
        
        response = self.client.get(url)
        
        assert response.status_code in [status.HTTP_404_NOT_FOUND, status.HTTP_403_FORBIDDEN]

    def test_business_cannot_manage_others_discounts(self):
        logger.info("TEST: Data Integrity - Owner A deleting Owner B's discount")
        owner_a = UserFactory(role=self.role_business)
        
        owner_b = UserFactory(role=self.role_business)
        biz_b = BusinessFactory(owner=owner_b)
        discount_b = DiscountFactory(business=biz_b)

        self.client.force_authenticate(user=owner_a)
        url = reverse('business-discount-detail', kwargs={'pk': discount_b.pk})
        
        response = self.client.delete(url)
        assert response.status_code in [status.HTTP_404_NOT_FOUND, status.HTTP_403_FORBIDDEN]

    # ============================================================================
    # 3. HIERARCHY & INTERNAL THREATS
    # ============================================================================

    def test_lower_admin_cannot_delete_super_admin(self):
        logger.info("TEST: Hierarchy - Moderator deleting Super Admin")
        role_mod = RoleFactory(name="Moderator", hierarchy_level=50)
        role_mod.permissions.add(self.perm_access_user_admin, self.perm_delete_user)

        role_super = RoleFactory(name="Super Admin", hierarchy_level=100)

        moderator = UserFactory(role=role_mod)
        super_admin = UserFactory(role=role_super)

        self.client.force_authenticate(user=moderator)
        url = reverse('admin-users-detail', kwargs={'pk': super_admin.pk})

        response = self.client.delete(url)
        assert response.status_code == status.HTTP_403_FORBIDDEN

    # ============================================================================
    # 4. PII PRIVACY & GUEST ACCESS
    # ============================================================================

    def test_business_cannot_create_data_for_other_business(self):
        logger.info("TEST: Tenant Injection - Owner A creating class for Business B")
        owner_a = UserFactory(role=self.role_business)
        owner_b = UserFactory(role=self.role_business)
        biz_b = BusinessFactory(owner=owner_b) # The victim

        self.client.force_authenticate(user=owner_a)
        
        url = reverse('business-class-list')
        
        # Owner A tries to create a class linked to Business B
        payload = {
            "title": "Malicious Class",
            "businessId": biz_b.businessId, # Injecting ID
            "description": "Spam",
            "status": "active"
        }
        
        response = self.client.post(url, payload)
        
        # If your permission class is strict, this fails. 
        # If your serializer ignores the passed ID and uses request.user.business, that's also safe.
        # But if it creates it under Biz B, that's a security hole.
        
        if response.status_code == 201:
            # Check who actually owns it
            new_id = response.data['classId']
            cls = ClassesMain.objects.get(pk=new_id)
            if cls.businessId == biz_b:
                pytest.fail("SECURITY FAIL: Owner A successfully created a class attached to Business B!")
        
        assert response.status_code in [403, 400]

    def test_user_cannot_view_others_booking(self):
        logger.info("TEST: PII Privacy - User A viewing User B's booking")
        user_a = UserFactory(role=self.role_student)
        user_b = UserFactory(role=self.role_student)
        booking_b = BookingFactory(user=user_b)
        
        self.client.force_authenticate(user=user_a)
        url = reverse('my-booking-detail', kwargs={'pk': booking_b.pk})
        
        response = self.client.get(url)
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_guest_cannot_access_booking_by_id(self):
        logger.info("TEST: Anonymous Access - Guest accessing booking by ID")
        booking = BookingFactory()
        self.client.logout()
        
        url = reverse('my-booking-detail', kwargs={'pk': booking.pk})
        response = self.client.get(url)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    # ============================================================================
    # 5. SPECIAL ENDPOINTS (Audit, Verification, Actions)
    # ============================================================================

    def test_student_cannot_read_audit_logs(self):
        logger.info("TEST: System Integrity - Student reading Audit Logs")
        student = UserFactory(role=self.role_student)
        AuditLogFactory()

        self.client.force_authenticate(user=student)
        url = reverse('admin-audit-logs-list')
        
        response = self.client.get(url)
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_user_cannot_approve_own_verification(self):
        logger.info("TEST: Verification Bypass - User approving own verification")
        user = UserFactory(role=self.role_student)
        req = VerificationRequestFactory(user=user)
        
        self.client.force_authenticate(user=user)
        url = reverse('admin-verification-process-verification', kwargs={'pk': req.pk})
        
        response = self.client.post(url, {"status": "approved"})
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_regular_admin_cannot_impersonate(self):
        logger.info("TEST: Custom Action - Regular Admin attempting Impersonation")
        role_reg = RoleFactory(name="Regular Admin", hierarchy_level=50)
        role_reg.permissions.add(self.perm_access_user_admin)
        
        reg_admin = UserFactory(role=role_reg)
        target = UserFactory()

        self.client.force_authenticate(user=reg_admin)
        url = reverse('admin-users-impersonate', kwargs={'pk': target.pk})
        
        response = self.client.post(url)
        assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.django_db
class TestAutomaticDoorCheck:
    """
    Crawls every registered ViewSet in the system to ensure NO endpoint 
    is accidentally left public (200 OK for Anonymous users).
    """

    def setup_method(self):
        self.client = APIClient()
        self.client.logout() # Ensure we are anonymous
        
        self.sensitive_routers = [
            (admin_router, "Admin"),
            (business_management_router, "Business"),
            (user_self_router, "User Self-Service")
        ]

    def test_all_sensitive_endpoints_are_protected(self):
        logger.info("========================================================")
        logger.info("STARTING AUTOMATIC ENDPOINT SECURITY CRAWL")
        logger.info("========================================================")
        
        failing_endpoints = []
        checked_count = 0

        for router, router_name in self.sensitive_routers:
            for prefix, viewset, basename in router.registry:
                # 1. Check List Endpoint
                try:
                    url_name = f"{basename}-list"
                    url = reverse(url_name)
                    self._check_url(url, router_name, viewset.__name__, failing_endpoints)
                    checked_count += 1
                except NoReverseMatch:
                    pass # Not all viewsets have lists

                # 2. Check Detail Endpoint (using a fake ID '999999' or uuid)
                # Note: We expect 401/403. If we get 404, it means we passed auth but didn't find obj.
                # Ideally, for security, we want 401/403 BEFORE 404.
                # However, many standard DRF setups return 404 for anonymous users on detail views 
                # if the permissions rely on 'get_object'. 
                # Ideally, permission_classes should block it before lookup.
                try:
                    url_name = f"{basename}-detail"
                    # Try int ID first, then UUID if it fails matching
                    try:
                        url = reverse(url_name, kwargs={'pk': 999999})
                    except NoReverseMatch:
                        url = reverse(url_name, kwargs={'pk': '00000000-0000-0000-0000-000000000000'})
                    
                    self._check_url(url, router_name, viewset.__name__, failing_endpoints)
                    checked_count += 1
                except NoReverseMatch:
                    pass

        logger.info(f"Crawl Complete. Checked {checked_count} endpoints.")

        if failing_endpoints:
            error_msg = "\nSECURITY ALERT: The following endpoints are PUBLICLY ACCESSIBLE:\n" + "\n".join(failing_endpoints)
            logger.error(error_msg)
            pytest.fail(error_msg)

    def _check_url(self, url, router_name, viewset_name, failing_list):
        """Helper to hit a URL and check status code"""
        response = self.client.get(url)
        
        status_code = response.status_code
        
        # We want to see 401 (Unauthorized) or 403 (Forbidden).
        # 405 (Method Not Allowed) is also acceptable (e.g. GET not allowed).
        # 404 (Not Found) implies it might be secure but just didn't find the dummy ID, 
        # OR it implies public access but empty DB. 
        # STRICT CHECK: We fail on 200.
        
        is_secure = status_code in [401, 403, 405]
        
        log_msg = f"[{router_name}] {viewset_name.ljust(30)} {url.ljust(40)} -> {status_code}"
        
        if status_code == 200:
            logger.critical(f"FAIL: {log_msg}")
            failing_list.append(f"{log_msg} (OPEN TO PUBLIC)")
        else:
            logger.info(f"PASS: {log_msg}")

    def test_all_custom_actions_are_protected(self):
        """
        Crawls all @action endpoints (e.g., 'impersonate', 'export', 'analytics')
        to ensure they are not public.
        """
        logger.info("========================================================")
        logger.info("STARTING CUSTOM ACTION SECURITY CRAWL")
        logger.info("========================================================")
        
        failing_endpoints = []
        checked_count = 0

        for router, router_name in self.sensitive_routers:
            for prefix, viewset, basename in router.registry:
                
                # DRF stores @action decorators here
                if not hasattr(viewset, 'get_extra_actions'):
                    continue
                
                for action_method in viewset.get_extra_actions():
                    url_name = f"{basename}-{action_method.url_name}"
                    
                    # Construct URL
                    try:
                        if action_method.detail:
                            # Needs an ID. Try int, then UUID.
                            try:
                                url = reverse(url_name, kwargs={'pk': 999999})
                            except NoReverseMatch:
                                url = reverse(url_name, kwargs={'pk': '00000000-0000-0000-0000-000000000000'})
                        else:
                            # List-level action (no ID needed)
                            url = reverse(url_name)
                        
                        # --- CORRECT LOGIC ---
                        # MethodMapper isn't a dict, so we check existence manually
                        if 'get' in action_method.mapping:
                            response = self.client.get(url)
                        elif 'post' in action_method.mapping:
                            response = self.client.post(url)
                        elif 'delete' in action_method.mapping:
                            response = self.client.delete(url)
                        elif 'patch' in action_method.mapping:
                            response = self.client.patch(url)
                        elif 'put' in action_method.mapping:
                            response = self.client.put(url)
                        else:
                            # Fallback
                            continue

                        # Check Status
                        status_code = response.status_code
                        log_msg = f"[{router_name}] Action: {action_method.url_name.ljust(20)} {url.ljust(40)} -> {status_code}"
                        
                        if status_code == 200:
                            logger.critical(f"FAIL: {log_msg}")
                            failing_endpoints.append(f"{log_msg} (OPEN TO PUBLIC)")
                        else:
                            logger.info(f"PASS: {log_msg}")
                        
                        checked_count += 1

                    except NoReverseMatch:
                        logger.warning(f"Could not reverse URL for action: {url_name}")

        logger.info(f"Custom Action Crawl Complete. Checked {checked_count} actions.")

        if failing_endpoints:
            pytest.fail("\nSECURITY ALERT: The following CUSTOM ACTIONS are PUBLIC:\n" + "\n".join(failing_endpoints))

@pytest.mark.django_db
class TestRoleBoundaries:
    def setup_method(self):
        self.client = APIClient()
        # Create a standard user
        self.student_role = RoleFactory(name="Student", hierarchy_level=1)
        self.student = UserFactory(role=self.student_role)
        self.client.force_authenticate(user=self.student)
        
        # Only check Admin routers
        self.admin_routers = [(admin_router, "Admin")]

    def test_student_cannot_access_admin_endpoints_full_methods(self):
        """
        Ensures a logged-in Student gets 403 Forbidden on ALL Admin endpoints
        across ALL HTTP methods (GET, POST, PUT, PATCH, DELETE).
        """
        failing_endpoints = []
        checked_count = 0
        
        for router, router_name in self.admin_routers:
            for prefix, viewset, basename in router.registry:
                
                # 1. Check LIST URL (GET, POST)
                try:
                    list_url = reverse(f"{basename}-list")
                    for method in ['get', 'post']:
                        self._check_method(method, list_url, failing_endpoints)
                        checked_count += 1
                except NoReverseMatch:
                    pass

                # 2. Check DETAIL URL (GET, PUT, PATCH, DELETE)
                # We use a dummy ID. If we get 404, that's acceptable (object not found).
                # But we prefer 403 (Forbidden) BEFORE looking for the object.
                # If we get 400 (Bad Request), that's a FAIL because we reached the serializer.
                try:
                    # Try int ID first, then UUID
                    try:
                        detail_url = reverse(f"{basename}-detail", kwargs={'pk': 999999})
                    except NoReverseMatch:
                        detail_url = reverse(f"{basename}-detail", kwargs={'pk': '00000000-0000-0000-0000-000000000000'})

                    for method in ['get', 'put', 'patch', 'delete']:
                        self._check_method(method, detail_url, failing_endpoints)
                        checked_count += 1
                except NoReverseMatch:
                    pass

        logger.info(f"Boundary Check Complete. Scanned {checked_count} method/endpoint combinations.")

        if failing_endpoints:
            pytest.fail("SECURITY ALERT: Student user accessed Admin endpoints:\n" + "\n".join(failing_endpoints))

    def _check_method(self, method_name, url, failing_list):
        """Helper to send request and check status"""
        # Send request (e.g., client.post(url))
        request_func = getattr(self.client, method_name)
        response = request_func(url)
        
        status = response.status_code
        
        # DEFINITION OF SECURE:
        # 403 Forbidden = Ideal (Permission blocked it)
        # 401 Unauthorized = Good (Auth failed)
        # 405 Method Not Allowed = Good (Endpoint doesn't support this method)
        # 404 Not Found = Acceptable (Authorized to look, but didn't find it. 
        #                  Ideally should be 403, but not a hard vulnerability unless data leaks.)
        
        # DEFINITION OF INSECURE:
        # 2xx (200, 201, 204) = CATASTROPHIC (Action performed)
        # 400 Bad Request = BAD (Permission passed, failed at data validation)
        
        is_insecure = status >= 200 and status < 300
        is_validation_error = status == 400

        if is_insecure:
            msg = f"[FAIL - ACCESS GRANTED] {method_name.upper()} {url} -> {status}"
            logger.critical(msg)
            failing_list.append(msg)
        elif is_validation_error:
            # 400 means "I checked your permissions, they are fine, but your data is ugly"
            # This is a vulnerability for Admin endpoints accessed by Students.
            msg = f"[FAIL - PERMISSION BYPASSED] {method_name.upper()} {url} -> {status} (Reached Serializer)"
            logger.error(msg)
            failing_list.append(msg)

from quickstart.urls import admin_router, business_management_router

@pytest.mark.django_db
class TestRoleBoundaries:
    def setup_method(self):
        self.client = APIClient()
        # Create a standard user (Student)
        self.student_role = RoleFactory(name="Student", hierarchy_level=1)
        self.student = UserFactory(role=self.student_role)
        self.client.force_authenticate(user=self.student)
        
        # Define Routers
        self.admin_routers = [(admin_router, "Admin")]
        self.business_routers = [(business_management_router, "Business Management")]

    def test_student_cannot_access_admin_endpoints_full_methods(self):
        """
        Ensures a logged-in Student gets 403 Forbidden on ALL Admin endpoints.
        """
        self._scan_routers(self.admin_routers, "Admin")

    def test_student_cannot_access_business_management_endpoints(self):
        """
        Ensures a logged-in Student gets 403 Forbidden on ALL Business Management endpoints.
        (Students should use public endpoints, not management ones).
        """
        self._scan_routers(self.business_routers, "Business")

    def _scan_routers(self, routers_list, scope_name):
        failing_endpoints = []
        checked_count = 0
        
        for router, router_name in routers_list:
            for prefix, viewset, basename in router.registry:
                
                # 1. Check LIST URL (GET, POST)
                try:
                    list_url = reverse(f"{basename}-list")
                    for method in ['get', 'post']:
                        self._check_method(method, list_url, failing_endpoints)
                        checked_count += 1
                except NoReverseMatch:
                    pass

                # 2. Check DETAIL URL (GET, PUT, PATCH, DELETE)
                try:
                    # Try int ID first, then UUID
                    try:
                        detail_url = reverse(f"{basename}-detail", kwargs={'pk': 999999})
                    except NoReverseMatch:
                        detail_url = reverse(f"{basename}-detail", kwargs={'pk': '00000000-0000-0000-0000-000000000000'})

                    for method in ['get', 'put', 'patch', 'delete']:
                        self._check_method(method, detail_url, failing_endpoints)
                        checked_count += 1
                except NoReverseMatch:
                    pass

        logger.info(f"[{scope_name}] Boundary Check Complete. Scanned {checked_count} combinations.")

        if failing_endpoints:
            pytest.fail(f"SECURITY ALERT: Student user accessed {scope_name} endpoints:\n" + "\n".join(failing_endpoints))

    def _check_method(self, method_name, url, failing_list):
        """Helper to send request and check status"""
        request_func = getattr(self.client, method_name)
        response = request_func(url)
        status = response.status_code
        
        # 403 = Secure (Forbidden)
        # 401 = Secure (Unauthorized)
        # 405 = Secure (Method Not Allowed)
        # 404 = Acceptable (Authorized but not found - ideally should be 403 first)
        
        # FAILURES:
        # 2xx = SUCCESS (Major Security Breach)
        # 400 = BAD REQUEST (Validation Error = Permission Bypassed)
        
        is_insecure = status >= 200 and status < 300
        is_validation_error = status == 400

        if is_insecure:
            msg = f"[FAIL - ACCESS GRANTED] {method_name.upper()} {url} -> {status}"
            logger.critical(msg)
            failing_list.append(msg)
        elif is_validation_error:
            msg = f"[FAIL - PERMISSION BYPASSED] {method_name.upper()} {url} -> {status} (Reached Serializer)"
            logger.error(msg)
            failing_list.append(msg)