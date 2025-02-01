from datetime import date, datetime, timedelta
from typing import List, Union
from django.utils import timezone
from django.db import models 

def generate_recurring_dates(
    start_date: Union[datetime, date],
    end_date_or_sessions: Union[date, int],
    recurrence_pattern: str,
    sessions_per_week: int
) -> List[datetime]:
    """
    Generate dates based on recurrence pattern and frequency.
    
    Args:
        start_date: Starting date
        end_date_or_sessions: Either end date (exclusive) or total number of sessions
        recurrence_pattern: 'weekly' or 'biweekly'
        sessions_per_week: Number of sessions per week (1-7)
    
    Returns:
        List of dates when sessions should occur
    """
    if isinstance(start_date, datetime):
        start_date = start_date.date()
    
    # Determine end condition
    if isinstance(end_date_or_sessions, int):
        # If total sessions is provided, calculate max possible end date
        max_end_date = start_date + timedelta(weeks=52)  # 1-year limit
        total_sessions_needed = end_date_or_sessions
        end_mode = 'sessions'
    else:
        # If end date is provided, convert to date
        max_end_date = end_date_or_sessions.date() if isinstance(end_date_or_sessions, datetime) else end_date_or_sessions
        total_sessions_needed = float('inf')
        end_mode = 'date'
        
    # Calculate days between sessions
    if recurrence_pattern == 'weekly':
        days_between = 7 // sessions_per_week
    else:  # biweekly
        days_between = 14 // sessions_per_week
        
    # Generate all possible dates
    dates = []
    current_date = start_date
    
    while current_date < max_end_date and len(dates) < total_sessions_needed:
        # For biweekly, skip alternate weeks
        if recurrence_pattern == 'biweekly':
            # Check if this is an "on" week
            weeks_since_start = ((current_date - start_date).days // 7)
            if weeks_since_start % 2 == 1:
                current_date += timedelta(days=7)
                continue
        
        # Add this date
        dates.append(current_date)
        
        # Move to next session date
        current_date += timedelta(days=days_between)
        
        # If we've added all sessions for this week, move to start of next week
        sessions_this_week = len(dates) % sessions_per_week
        if sessions_this_week == 0:
            # Move to start of next week
            if recurrence_pattern == 'weekly':
                current_date = start_date + timedelta(weeks=len(dates)//sessions_per_week)
            else:  # biweekly
                current_date = start_date + timedelta(weeks=2 * (len(dates)//sessions_per_week))
        
        # Break if we've reached total sessions in sessions mode
        if end_mode == 'sessions' and len(dates) >= total_sessions_needed:
            break
    
    return dates

def is_date_available(date, schedule) -> bool:
    """
    Check if a specific date is available for scheduling.
    
    Args:
        date: Date to check
        schedule: Schedule object to check availability against
    
    Returns:
        Boolean indicating if the date is available
    """
    # Check if date falls on correct day of week
    if date.strftime('%a')[:3] != schedule.day:
        return False
        
    # Check if date is in any break periods
    break_periods = schedule.breaks.filter(
        start_date__lte=date,
        end_date__gte=date
    )
    if break_periods.exists():
        return False
        
    # Check if there's a cancelled instance on this date
    cancelled_instances = schedule.instances.filter(
        date=date,
        status='cancelled'
    )
    if cancelled_instances.exists():
        return False
    
    return True

def calculate_next_occurrence(schedule, from_date=None):
    """
    Calculate the next occurrence of a schedule after a given date.
    
    Args:
        schedule: Schedule object
        from_date: Date to calculate from (defaults to current date)
    
    Returns:
        Date of next available occurrence
    """
    if from_date is None:
        from_date = timezone.now().date()
        
    # Get day of week as number (0 = Monday)
    current_day = from_date.weekday()
    target_day = {
        'Mon': 0, 'Tue': 1, 'Wed': 2, 'Thu': 3,
        'Fri': 4, 'Sat': 5, 'Sun': 6
    }[schedule.day]
    
    # Calculate days until next occurrence
    days_ahead = target_day - current_day
    if days_ahead <= 0:  # Target day has passed this week
        days_ahead += 7
        
    next_date = from_date + timedelta(days=days_ahead)
    
    # Keep looking for next available date if this one isn't available
    while not is_date_available(next_date, schedule):
        next_date += timedelta(days=7)
        
    return next_date

def get_available_capacity(schedule, date):
    """
    Get available spots for a schedule on a specific date.
    
    Args:
        schedule: Schedule object
        date: Date to check capacity for
    
    Returns:
        Number of available spots
    """
    # Use models.Sum instead of sum
    booked = schedule.bookings.filter(
        booking_date=date,
        status__in=['confirmed', 'pending']
    ).aggregate(
        total=models.Sum('participants')
    )['total'] or 0
    
    return max(0, schedule.effective_max_participants - booked)