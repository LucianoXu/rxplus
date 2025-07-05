
import traceback

from typing import Any

class TaggedData:
    """
    A class to hold data with a tag.
    
    Attributes:
        tag (str): The tag of the data.
        data (any): The data itself.
    """
    def __init__(self, tag: str, data: Any):
        self.tag = tag
        self.data = data

    def __repr__(self) -> str:
        return f"(tag={self.tag}, {repr(self.data)})"
    
    def __str__(self) -> str:
        return f"(tag={self.tag}, {str(self.data)})"

def get_short_error_info(e: Exception) -> str:
    """
    Get a short error information from an exception.
    
    Args:
        e (Exception): The exception to get the error information from.
    
    Returns:
        str: A short error information.
    """
    return f"{type(e).__name__}: {str(e)}"

# the function to get the full error information from an exception.
def get_full_error_info(e: Exception) -> str:
    """
    Get the full error information from an exception.
    
    Args:
        e (Exception): The exception to get the error information from.
    
    Returns:
        str: The full error information.
    """
    return ''.join(traceback.format_exception(type(e), e, e.__traceback__))