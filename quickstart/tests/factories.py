import factory
from factory.django import DjangoModelFactory
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission, ContentType
import datetime
from django.utils import timezone

from quickstart.models import (
    Role, 
    BusinessInfo, 
    ClassesMain, 
    ClassCategory,
    ClassOption,
    Schedule,
    ScheduleInstance,
    Booking,
    Payout,
    Discount,
    AuditLog,
    VerificationRequest,
    BusinessStaff
)

User = get_user_model()

class PermissionFactory(DjangoModelFactory):
    class Meta:
        model = Permission
        django_get_or_create = ('codename', 'content_type')

    name = factory.Sequence(lambda n: f"Permission {n}")
    codename = factory.Sequence(lambda n: f"perm_{n}")
    content_type = factory.Iterator(ContentType.objects.all())

class RoleFactory(DjangoModelFactory):
    class Meta:
        model = Role
        django_get_or_create = ('name',)
        # Silence factory_boy warning about post_generation save
        skip_postgeneration_save = True  

    name = "Standard User"
    hierarchy_level = 1

    @factory.post_generation
    def permissions(self, create, extracted, **kwargs):
        if not create or not extracted:
            return
        self.permissions.add(*extracted)

class UserFactory(DjangoModelFactory):
    class Meta:
        model = User
        # Silence factory_boy warning
        skip_postgeneration_save = True 

    email = factory.Sequence(lambda n: f"user{n}@example.com")
    username = factory.Sequence(lambda n: f"user{n}")
    first_name = "John"
    last_name = "Doe"
    role = factory.SubFactory(RoleFactory)
    is_active = True

    @factory.post_generation
    def password(self, create, extracted, **kwargs):
        password = extracted or "password"
        self.set_password(password)
        if create:
            self.save()

class BusinessFactory(DjangoModelFactory):
    class Meta:
        model = BusinessInfo

    owner = factory.SubFactory(UserFactory)
    businessName = factory.Sequence(lambda n: f"Business {n}")
    studentContactEmail = factory.Sequence(lambda n: f"biz{n}@example.com")
    studentContactPhone = "555-0199"
    isActive = True
    verificationStatus = "verified"
    slug = factory.Sequence(lambda n: f"business-{n}")

class ClassCategoryFactory(DjangoModelFactory):
    class Meta:
        model = ClassCategory
    
    name = "Art"
    key = "art"

class ClassFactory(DjangoModelFactory):
    class Meta:
        model = ClassesMain

    businessId = factory.SubFactory(BusinessFactory)
    title = factory.Sequence(lambda n: f"Pottery Class {n}")
    description = "Learn to throw clay."
    category = factory.SubFactory(ClassCategoryFactory)
    status = "active"
    location = "Studio A"
    coordinates = "40.7128,-74.0060"

class ClassOptionFactory(DjangoModelFactory):
    class Meta:
        model = ClassOption
    
    classId = factory.SubFactory(ClassFactory)
    booking_type = "Single Session"
    price_type = "per_session"

class ScheduleFactory(DjangoModelFactory):
    class Meta:
        model = Schedule

    option = factory.SubFactory(ClassOptionFactory)
    time = datetime.time(10, 0)
    duration = 60
    price = 20.00
    maxParticipants = 10
    # Required for 'Single Session' validation logic in your model
    date = factory.LazyFunction(datetime.date.today)

class ScheduleInstanceFactory(DjangoModelFactory):
    class Meta:
        model = ScheduleInstance

    schedule = factory.SubFactory(ScheduleFactory)
    # Ensure instance date matches schedule date
    date = factory.LazyAttribute(lambda o: o.schedule.date)
    time = datetime.time(10, 0)
    max_participants = 10
    price = 20.00
    status = "scheduled"

class BookingFactory(DjangoModelFactory):
    class Meta:
        model = Booking

    user = factory.SubFactory(UserFactory)
    schedule_instance = factory.SubFactory(ScheduleInstanceFactory)
    status = "confirmed"
    payment_status = "paid"
    amount_paid = 20.00
    participants = 1

class PayoutFactory(DjangoModelFactory):
    class Meta:
        model = Payout

    business = factory.SubFactory(BusinessFactory)
    stripe_transfer_id = factory.Sequence(lambda n: f"tr_{n}")
    amount = 100.00
    currency = "CAD"
    status = "paid"

class DiscountFactory(DjangoModelFactory):
    class Meta:
        model = Discount
    
    business = factory.SubFactory(BusinessFactory)
    name = "Summer Sale"
    value = 10.00
    discount_type = "fixed_amount"
    # Ensure uniqueness of code if provided, or leave blank for automatic
    code = factory.Sequence(lambda n: f"SALE{n}")

class AuditLogFactory(DjangoModelFactory):
    class Meta:
        model = AuditLog
    
    user_email = "admin@test.com"
    action = "login"

class VerificationRequestFactory(DjangoModelFactory):
    class Meta:
        model = VerificationRequest
    
    user = factory.SubFactory(UserFactory)
    business = factory.SubFactory(BusinessFactory)
    status = "pending"