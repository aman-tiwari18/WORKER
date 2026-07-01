# # backend/schemas.py

#working to start all and end all

# from pydantic import BaseModel
# from typing import List

# class StartRequest(BaseModel):
#     analytics: List[str]  # future use (crowd, intruder etc.)

# class StatusResponse(BaseModel):
#     status: str

# class MessageResponse(BaseModel):
#     message: str









#seelcted start working properly but stop is not woprking


# from pydantic import BaseModel
# from typing import List

# class StartRequest(BaseModel):
#     analytics: List[str]

# class StatusResponse(BaseModel):
#     running: bool
#     analytics: List[str]

# class MessageResponse(BaseModel):
#     message: str













#all start / all stop wali functionality added nhi he but selected start and stop is working properly(lag)
# from pydantic import BaseModel
# from typing import List

# class StartRequest(BaseModel):
#     analytics: List[str]

# class StopRequest(BaseModel):
#     analytics: List[str]

# class StatusResponse(BaseModel):
#     running: List[str]
#     stopped: List[str]

# class MessageResponse(BaseModel):
#     message: str
















from pydantic import BaseModel
from typing import List, Optional

class StartRequest(BaseModel):
    analytics: Optional[List[str]] = None
    all: Optional[bool] = False

class StopRequest(BaseModel):
    analytics: Optional[List[str]] = None
    all: Optional[bool] = False

class StatusResponse(BaseModel):
    running: List[str]
    stopped: List[str]

class MessageResponse(BaseModel):
    message: str

