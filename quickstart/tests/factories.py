# quickstart/tests/factories.py

import factory
from factory.django import DjangoModelFactory
from django.utils import timezone
from datetime import date, time
from decimal import Decimal

# CHANGE THIS:
# from ..models import (
# TO THIS (absolute import):
from quickstart.models import (
    CustomUser,
    BusinessInfo,
    ClassCategory,
    ClassesMain,
    ClassOption,
    Schedule,
    ScheduleInstance,
    Booking,
    Reviews,
    Role,
)

# --- Core Factories ---
class RoleFactory(DjangoModelFactory):
    class Meta:
        model = Role
    
    name = factory.Faker('job')
    hierarchy_level = factory.Sequence(lambda n: n)

class UserFactory(DjangoModelFactory):
    class Meta:
        model = CustomUser
        django_get_or_create = ('username',)

    username = factory.Faker('user_name')
    email = factory.LazyAttribute(lambda o: f"{o.username}@example.com")
    first_name = factory.Faker('first_name')
    last_name = factory.Faker('last_name')
    is_staff = False
    is_active = True

class ClassCategoryFactory(DjangoModelFactory):
    class Meta:
        model = ClassCategory
        django_get_or_create = ('key',)
    
    name = factory.Faker('word')
    key = factory.LazyAttribute(lambda o: o.name.lower())

class BusinessInfoFactory(DjangoModelFactory):
    class Meta:
        model = BusinessInfo

    owner = factory.SubFactory(UserFactory)
    businessName = factory.Faker('company')
    businessType = 'school'
    businessDescription = factory.Faker('text', max_nb_chars=300)
    studentContactPhone = factory.Faker('phone_number')
    studentContactEmail = factory.Faker('email')
    preferredContact = 'email'
    businessAddress = factory.Faker('street_address')
    businessCity = factory.Faker('city')
    businessState = factory.Faker('state_abbr')
    businessZipCode = factory.Faker('zipcode')
    openingTime = time(9, 0)
    closingTime = time(17, 0)
    classCategory = factory.SubFactory(ClassCategoryFactory)
    termsAccepted = True
    privacyAccepted = True
    verificationStatus = 'verified'
    isActive = True
    business_timezone = 'UTC'
    stripe_account_id = factory.Sequence(lambda n: f"acct_test_{n}")
    stripe_account_status = 'active'

# --- Class and Scheduling Factories ---
class ClassesMainFactory(DjangoModelFactory):
    class Meta:
        model = ClassesMain

    businessId = factory.SubFactory(BusinessInfoFactory)
    title = factory.Faker('catch_phrase')
    description = factory.Faker('text', max_nb_chars=500)
    category = factory.LazyAttribute(lambda o: o.businessId.classCategory)
    status = 'active'
    location = "Test Location"
    coordinates = "45.0000,-75.0000"

class ClassOptionFactory(DjangoModelFactory):
    class Meta:
        model = ClassOption

    classId = factory.SubFactory(ClassesMainFactory)
    booking_type = 'Single Session'
    cancellationPolicy = '24h'
    cancellationRefundPercentage = 100

class ScheduleFactory(DjangoModelFactory):
    class Meta:
        model = Schedule
    
    option = factory.SubFactory(ClassOptionFactory)
    # Default to a single session today
    date = factory.LazyFunction(timezone.now().date)
    time = time(14, 0)
    duration = 60
    price = Decimal('25.00')
    maxParticipants = 10

class ScheduleInstanceFactory(DjangoModelFactory):
    class Meta:
        model = ScheduleInstance

    schedule = factory.SubFactory(ScheduleFactory)
    date = factory.LazyAttribute(lambda o: o.schedule.date or timezone.now().date())
    time = factory.LazyAttribute(lambda o: o.schedule.time)
    duration = factory.LazyAttribute(lambda o: o.schedule.duration)
    price = factory.LazyAttribute(lambda o: o.schedule.price)
    max_participants = factory.LazyAttribute(lambda o: o.schedule.maxParticipants)
    status = 'scheduled'

# --- Interaction Factories ---
class BookingFactory(DjangoModelFactory):
    class Meta:
        model = Booking

    schedule_instance = factory.SubFactory(ScheduleInstanceFactory)
    user = factory.SubFactory(UserFactory)
    status = 'confirmed'
    participants = 1
    amount_paid = factory.LazyAttribute(lambda o: o.schedule_instance.price * o.participants)
    payment_status = 'paid'

class ReviewFactory(DjangoModelFactory):
    class Meta:
        model = Reviews

    userId = factory.SubFactory(UserFactory)
    classId = factory.SubFactory(ClassesMainFactory)
    businessId = factory.SelfAttribute('classId.businessId')
    booking = factory.SubFactory(BookingFactory, userId=factory.SelfAttribute('..userId'), schedule_instance__schedule__option__classId=factory.SelfAttribute('..classId'))
    rating = factory.Faker('pyint', min_value=4, max_value=5)
    comment = factory.Faker('text')
    status = 'approved'