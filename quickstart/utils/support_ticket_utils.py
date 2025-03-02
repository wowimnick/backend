import re
import logging
from quickstart.models import SupportTicket

logger = logging.getLogger(__name__)

def parse_ticket_data(ticket_text):
    """
    Parse ticket data from the ticket text block provided by AI
    """
    # Initialize default values
    ticket_data = {
        'category': 'other',
        'subject': 'Support Request',
        'description': 'No details provided',
        'priority': 'medium'
    }
    
    # Extract field values using regex
    category_match = re.search(r'category:\s*(.*?)(?:\n|$)', ticket_text)
    if category_match:
        category = category_match.group(1).strip().lower()
        valid_categories = ['account', 'booking', 'payment', 'technical', 'feature', 'other']
        if category in valid_categories:
            ticket_data['category'] = category
        
    subject_match = re.search(r'subject:\s*(.*?)(?:\n|$)', ticket_text)
    if subject_match:
        ticket_data['subject'] = subject_match.group(1).strip()
        
    description_match = re.search(r'description:\s*(.*?)(?:(?=priority:)|$)', ticket_text, re.DOTALL)
    if description_match:
        ticket_data['description'] = description_match.group(1).strip()
        
    priority_match = re.search(r'priority:\s*(.*?)(?:\n|$)', ticket_text)
    if priority_match:
        priority = priority_match.group(1).strip().lower()
        if priority in ['low', 'medium', 'high', 'urgent']:
            ticket_data['priority'] = priority
    
    return ticket_data

def process_for_ticket_creation(ai_message, user, chat_session):
    """
    Process AI message to detect and handle ticket creation requests
    """
    ticket_pattern = r'<CREATE_TICKET>(.*?)</CREATE_TICKET>'
    match = re.search(ticket_pattern, ai_message, re.DOTALL)
    
    if not match:
        return ai_message, None
        
    # Extract ticket info from matching text
    ticket_text = match.group(1)
    ticket_data = parse_ticket_data(ticket_text)
    
    # Create the ticket
    ticket = SupportTicket.objects.create(
        user=user,
        chat_session=chat_session,
        category=ticket_data['category'],
        subject=ticket_data['subject'],
        description=ticket_data['description'],
        priority=ticket_data['priority']
    )
    
    # Short confirmation message for the AI response
    confirmation = "I've created a support ticket for you! You'll see the details below."
    
    # Check if the ticket tags are the only content in the message
    if ai_message.strip() == match.group(0).strip():
        # Replace the entire message with just the confirmation
        processed_message = confirmation
    else:
        # Replace the tags with the confirmation
        processed_message = ai_message.replace(match.group(0), confirmation)
    
    return processed_message, ticket