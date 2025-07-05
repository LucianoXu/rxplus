import traceback

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