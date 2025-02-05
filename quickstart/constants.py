from django.db import models

class BookingChoices:
    SINGLE = 'Single Session'
    RECURRING = 'Recurring Classes'
    COURSE = 'Full Course'
    
    CHOICES = [
        (SINGLE, 'Single Session'),
        (RECURRING, 'Recurring Classes'),
        (COURSE, 'Full Course')
    ]

class BookingStatus:
    PENDING = 'pending'
    CONFIRMED = 'confirmed'
    CANCELLED = 'cancelled'
    COMPLETED = 'completed'
    
    CHOICES = [
        (PENDING, 'Pending'),
        (CONFIRMED, 'Confirmed'),
        (CANCELLED, 'Cancelled'),
        (COMPLETED, 'Completed')
    ]

class PaymentStatus:
    PENDING = 'pending'
    PAID = 'paid'
    REFUNDED = 'refunded'
    
    CHOICES = [
        (PENDING, 'Pending'),
        (PAID, 'Paid'),
        (REFUNDED, 'Refunded')
    ]