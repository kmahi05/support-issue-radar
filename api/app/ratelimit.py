from slowapi import Limiter
from slowapi.util import get_remote_address

# In-memory storage: fine for one API process (PRD §6 scaling path).
limiter = Limiter(key_func=get_remote_address)
