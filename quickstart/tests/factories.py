"""
Factory Boy factories for quickstart models.
Used to create test data for public endpoint tests; data is isolated per test via DB transactions.
"""
import uuid
from decimal import Decimal
from datetime import date, time, timedelta
from django.utils import timezone

import factory
from factory.django import DjangoModelFactory

from quickstart.models import (
    CustomUser,
    BusinessInfo,
    Contact,
    ClassCategory,
    ClassSubcategory,
    ClassesMain,
    ClassOption,
    Schedule,
    ScheduleInstance,
    Booking,
    GiftCard,
    BlogCategory,
    BlogPost,
    PartnerTier,
)


class UserFactory(DjangoModelFactory):
    class Meta:
        model = CustomUser
        django_get_or_create = ("email",)

    username = factory.Sequence(lambda n: f"user{n}@example.com")
    email = factory.Sequence(lambda n: f"user{n}@example.com")
    first_name = factory.Faker("first_name")
    last_name = factory.Faker("last_name")
    country = "Canada"
    city = "Toronto"
    state = "ON"
    address = "123 Test St"
    zipCode = "M5V 1A1"
    is_active = True
    is_staff = False
    is_superuser = False


class PartnerTierFactory(DjangoModelFactory):
    class Meta:
        model = PartnerTier
        django_get_or_create = ("name",)

    name = factory.Sequence(lambda n: f"Tier {n}")
    fee_percentage = Decimal("13.00")
    is_default = False


class BusinessFactory(DjangoModelFactory):
    class Meta:
        model = BusinessInfo

    owner = factory.SubFactory(UserFactory)
    businessName = factory.Sequence(lambda n: f"Test Business {n}")
    slug = factory.Sequence(lambda n: f"test-business-{n}")
    businessType = "venue"
    businessDescription = "Test description"
    studentContactPhone = "+15551234567"
    studentContactEmail = factory.LazyAttribute(lambda o: o.owner.email)
    preferredContact = "email"
    businessAddress = "123 Main St"
    businessCity = "Toronto"
    businessState = "ON"
    businessZipCode = "M5V 1A1"
    isActive = True
    verificationStatus = "verified"
    termsAccepted = True
    privacyAccepted = True
    business_timezone = "America/Toronto"


class ContactFactory(DjangoModelFactory):
    class Meta:
        model = Contact

    business = factory.SubFactory(BusinessFactory)
    first_name = factory.Faker("first_name")
    last_name = factory.Faker("last_name")
    email = factory.Sequence(lambda n: f"guest{n}@example.com")
    phone_number = "+15559876543"
    source = "guest_booking"
    user = None


class ClassCategoryFactory(DjangoModelFactory):
    class Meta:
        model = ClassCategory
        django_get_or_create = ("key",)

    name = factory.Sequence(lambda n: f"Category {n}")
    key = factory.Sequence(lambda n: f"category-{n}")
    is_featured = False
    sort_order = 0


class ClassSubcategoryFactory(DjangoModelFactory):
    class Meta:
        model = ClassSubcategory

    category = factory.SubFactory(ClassCategoryFactory)
    name = factory.Sequence(lambda n: f"Subcategory {n}")
    key = factory.Sequence(lambda n: f"subcat-{n}")


class ClassMainFactory(DjangoModelFactory):
    class Meta:
        model = ClassesMain

    businessId = factory.SubFactory(BusinessFactory)
    title = factory.Sequence(lambda n: f"Test Class {n}")
    slug = factory.Sequence(lambda n: f"test-class-{n}")
    description = "A test class description."
    features = []
    category = factory.SubFactory(ClassCategoryFactory)
    subcategory = None
    status = "active"
    location = "123 Studio St"
    city = "Toronto"
    state = "ON"
    coordinates = "43.6532,-79.3832"


class ClassOptionFactory(DjangoModelFactory):
    class Meta:
        model = ClassOption

    classId = factory.SubFactory(ClassMainFactory)
    title = "General Admission"
    booking_type = "Single Session"
    cancellationPolicy = "flexible"
    price_type = "per_session"
    level = "all"


class ScheduleFactory(DjangoModelFactory):
    class Meta:
        model = Schedule

    option = factory.SubFactory(ClassOptionFactory)
    # Single-session Schedule.clean() requires date; day is set from date in save().
    date = factory.LazyFunction(lambda: date.today() + timedelta(days=7))
    day = "Sat"
    time = time(10, 0)
    duration = 60
    price = Decimal("25.00")
    maxParticipants = 10
    minParticipants = 1


class ScheduleInstanceFactory(DjangoModelFactory):
    class Meta:
        model = ScheduleInstance

    schedule = factory.SubFactory(ScheduleFactory)
    date = factory.LazyFunction(lambda: date.today() + timedelta(days=7))
    time = time(10, 0)
    duration = 60
    price = Decimal("25.00")
    max_participants = 10
    min_participants = 1
    status = "scheduled"


class BookingFactory(DjangoModelFactory):
    class Meta:
        model = Booking

    schedule_instance = factory.SubFactory(ScheduleInstanceFactory)
    user = None
    contact = factory.SubFactory(ContactFactory)
    participants = 1
    amount_paid = Decimal("25.00")
    payment_status = "paid"
    status = "confirmed"
    enrollment_type = "Single Session"
    cancellation_policy = "flexible"
    cancellation_token = factory.LazyFunction(uuid.uuid4)

    @factory.post_generation
    def link_contact_to_business(obj, create, extracted, **kwargs):
        if create and obj.contact and obj.schedule_instance:
            business = obj.schedule_instance.schedule.option.classId.businessId
            obj.contact.business = business
            obj.contact.save(update_fields=["business"])


class GiftCardFactory(DjangoModelFactory):
    class Meta:
        model = GiftCard

    initial_amount = Decimal("50.00")
    current_balance = Decimal("50.00")
    currency = "CAD"
    recipient_email = factory.Faker("email")
    recipient_name = factory.Faker("name")
    sender_name = factory.Faker("name")
    message = "Happy testing!"
    is_active = True
    code = factory.LazyFunction(
        lambda: "TEST-" + uuid.uuid4().hex[:4].upper() + "-" + uuid.uuid4().hex[:4].upper()
    )


class BlogCategoryFactory(DjangoModelFactory):
    class Meta:
        model = BlogCategory
        django_get_or_create = ("slug",)

    name = factory.Sequence(lambda n: f"Blog Category {n}")
    slug = factory.Sequence(lambda n: f"blog-cat-{n}")


class BlogPostFactory(DjangoModelFactory):
    class Meta:
        model = BlogPost

    title = factory.Sequence(lambda n: f"Blog Post {n}")
    slug = factory.Sequence(lambda n: f"blog-post-{n}")
    excerpt = "Short excerpt."
    content = "<p>Full content here.</p>"
    image_url = "https://example.com/image.jpg"
    author = factory.SubFactory(UserFactory)
    category = factory.SubFactory(BlogCategoryFactory)
    status = "published"
    tags = []
