"""
Voice Control Assistant for AndyOS Reminders
Enables setting reminders via natural language voice commands.

Supports commands like:
- "set a reminder on 10th may at 7pm and name it mothers day"
- "remind me to buy milk on 15th at 2 pm"
- "create reminder for doctor appointment on 20th june at 9 am, high priority"
"""

import re
from datetime import datetime, date, timedelta
import calendar

# Try to import speech recognition libraries
try:
    import speech_recognition as sr
    SPEECH_RECOGNITION_AVAILABLE = True
except ImportError:
    SPEECH_RECOGNITION_AVAILABLE = False

# Month name mapping
MONTH_NAMES = {name.lower(): num for num, name in enumerate(calendar.month_name) if name}


class ReminderVoiceParser:
    """Parse natural language voice commands into reminder parameters."""
    
    def __init__(self):
        self.recognizer = sr.Recognizer() if SPEECH_RECOGNITION_AVAILABLE else None
    
    @staticmethod
    def parse_time_from_text(text):
        """
        Extract time from text (e.g., '7pm', '7:00 pm', '19:00')
        Returns time as HH:MM format or None if not found.
        """
        # Match patterns like "7pm", "7 pm", "7:00 pm", "19:00"
        time_patterns = [
            r'(\d{1,2}):(\d{2})\s*(am|pm)?',  # HH:MM format
            r'(\d{1,2})\s*(am|pm)',            # H am/pm format
            r'(\d{1,2}):(\d{2})\s*(am|pm)',   # HH:MM am/pm
        ]
        
        for pattern in time_patterns:
            match = re.search(pattern, text.lower())
            if match:
                if len(match.groups()) == 2:
                    # Pattern: H am/pm
                    hour = int(match.group(1))
                    meridiem = match.group(2).lower() if match.group(2) else 'am'
                    minute = 0
                else:
                    # Pattern: HH:MM with optional am/pm
                    hour = int(match.group(1))
                    minute = int(match.group(2)) if match.group(2) else 0
                    meridiem = match.group(3).lower() if match.group(3) else 'am'
                
                # Convert to 24-hour format
                if meridiem == 'pm' and hour != 12:
                    hour += 12
                elif meridiem == 'am' and hour == 12:
                    hour = 0
                
                return f"{hour:02d}:{minute:02d}"
        
        return None
    
    @staticmethod
    def parse_date_from_text(text):
        """
        Extract date from text (e.g., '10th may', '15th', 'tomorrow', 'next monday')
        Returns date as YYYY-MM-DD format or None if not found.
        """
        text_lower = text.lower()
        today = date.today()
        
        # Special cases
        if 'today' in text_lower:
            return str(today)
        if 'tomorrow' in text_lower:
            return str(today + timedelta(days=1))
        if 'next week' in text_lower or 'in a week' in text_lower:
            return str(today + timedelta(days=7))
        
        # Pattern: day + month (e.g., "10th may", "15 june")
        day_month_pattern = r'(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+)'
        match = re.search(day_month_pattern, text_lower)
        if match:
            day = int(match.group(1))
            month_str = match.group(2).strip()
            month = MONTH_NAMES.get(month_str)
            
            if month:
                # Determine year
                target_date = date(today.year, month, day)
                # If the date has already passed this year, use next year
                if target_date < today:
                    target_date = date(today.year + 1, month, day)
                return str(target_date)
        
        # Pattern: just day (e.g., "10th", "15")
        day_pattern = r'(?:on\s+)?(\d{1,2})(?:st|nd|rd|th)?(?:\s|$)'
        match = re.search(day_pattern, text_lower)
        if match:
            day = int(match.group(1))
            try:
                # Assume current month
                target_date = date(today.year, today.month, day)
                if target_date < today:
                    # If the day has passed, move to next month
                    if today.month == 12:
                        target_date = date(today.year + 1, 1, day)
                    else:
                        target_date = date(today.year, today.month + 1, day)
                return str(target_date)
            except ValueError:
                return None
        
        return None
    
    @staticmethod
    def parse_priority_from_text(text):
        """
        Extract priority from text (e.g., 'high priority', 'urgent', 'low')
        Returns 'high', 'medium', or 'low'.
        """
        text_lower = text.lower()
        
        if any(word in text_lower for word in ['high', 'urgent', 'critical', 'important']):
            return 'high'
        elif any(word in text_lower for word in ['low', 'later']):
            return 'low'
        
        return 'medium'  # Default
    
    @staticmethod
    def extract_reminder_title(text):
        """
        Extract the reminder title/name from the command.
        Handles patterns like "name it mothers day" or just extracts the main subject.
        """
        # Pattern: "name it <title>" or "call it <title>"
        name_pattern = r'(?:name|call)\s+it\s+([^,]+?)(?:\s+(?:high|medium|low|priority|on|at)|\s*$)'
        match = re.search(name_pattern, text.lower())
        if match:
            return match.group(1).strip()
        
        # Try to extract from "reminder for <title>"
        for_pattern = r'reminder\s+(?:for|to)\s+([^,]+?)(?:\s+on|\s+at|$)'
        match = re.search(for_pattern, text.lower())
        if match:
            return match.group(1).strip()
        
        # Fallback: just return a portion of the text
        # Remove date/time patterns and return what's left
        cleaned = re.sub(r'\d{1,2}(?:st|nd|rd|th)?\s+[a-z]+', '', text.lower()).strip()
        cleaned = re.sub(r'(?:at|on)\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?', '', cleaned).strip()
        cleaned = re.sub(r'(?:remind|set|create)\s+(?:me\s+)?(?:a\s+)?reminder', '', cleaned).strip()
        cleaned = re.sub(r'(?:name|call)\s+it', '', cleaned).strip()
        
        return cleaned.strip() if cleaned else 'Reminder'
    
    @classmethod
    def parse_voice_command(cls, command_text):
        """
        Parse a voice command and extract reminder parameters.
        
        Args:
            command_text: Natural language command string
        
        Returns:
            Dictionary with keys: title, due_date, reminder_time, priority, notes
            Or None if parsing fails
        """
        if not command_text or not isinstance(command_text, str):
            return None
        
        try:
            # Extract components
            title = cls.extract_reminder_title(command_text)
            due_date = cls.parse_date_from_text(command_text)
            reminder_time = cls.parse_time_from_text(command_text)
            priority = cls.parse_priority_from_text(command_text)
            
            # Validate required fields
            if not due_date:
                # If no date found, use tomorrow
                due_date = str(date.today() + timedelta(days=1))
            
            if not reminder_time:
                # If no time found, use 9:00 AM
                reminder_time = "09:00"
            
            return {
                'title': title,
                'due_date': due_date,
                'reminder_time': reminder_time,
                'priority': priority,
                'notes': f'Voice command: {command_text}'
            }
        except Exception as e:
            print(f"Error parsing voice command: {e}")
            return None
    
    def listen_and_parse(self, timeout=10):
        """
        Listen to microphone input and parse the command.
        
        Args:
            timeout: Maximum seconds to listen
        
        Returns:
            Parsed reminder parameters or None if failed
        """
        if not SPEECH_RECOGNITION_AVAILABLE or not self.recognizer:
            return None
        
        try:
            with sr.Microphone() as source:
                print("Listening for voice command...")
                self.recognizer.adjust_for_ambient_noise(source, duration=0.5)
                audio = self.recognizer.listen(source, timeout=timeout, phrase_time_limit=timeout)
            
            # Try multiple speech recognition services
            try:
                text = self.recognizer.recognize_google(audio)
            except sr.UnknownValueError:
                try:
                    # Fallback to Sphinx if Google fails
                    text = self.recognizer.recognize_sphinx(audio)
                except:
                    return None
            
            print(f"Recognized: {text}")
            return self.parse_voice_command(text)
        
        except sr.RequestError as e:
            print(f"Speech recognition service error: {e}")
        except sr.UnknownValueError:
            print("Could not understand audio")
        except Exception as e:
            print(f"Voice listening error: {e}")
        
        return None


def create_reminder_from_voice_command(conn, command_text):
    """
    Main function to create a reminder from a voice command.
    
    Args:
        conn: Database connection
        command_text: Voice command string
    
    Returns:
        Dictionary with result status and message
    """
    parser = ReminderVoiceParser()
    parsed = parser.parse_voice_command(command_text)
    
    if not parsed:
        return {
            'success': False,
            'message': 'Could not understand the voice command. Please try again.'
        }
    
    try:
        # Validate required fields one more time
        if not parsed.get('title') or not parsed.get('due_date'):
            return {
                'success': False,
                'message': 'Missing reminder title or date. Please specify both.'
            }
        
        # Insert into database
        conn.execute("""
            INSERT INTO reminders
            (title, due_date, priority, recurrence, notes)
            VALUES (?, ?, ?, ?, ?)
        """, (
            parsed['title'],
            parsed['due_date'],
            parsed.get('priority', 'medium'),
            None,  # No recurrence for voice commands by default
            parsed.get('notes', '')
        ))
        conn.commit()
        
        return {
            'success': True,
            'message': f"Reminder '{parsed['title']}' set for {parsed['due_date']} at {parsed['reminder_time']}",
            'reminder': parsed
        }
    
    except Exception as e:
        conn.rollback()
        return {
            'success': False,
            'message': f'Error creating reminder: {str(e)}'
        }
