from fastapi import FastAPI, HTTPException
from motor.motor_asyncio import AsyncIOMotorClient
import os
from dotenv import load_dotenv
from bson import ObjectId
from datetime import datetime
from fastapi import FastAPI
from fastapi_socketio import SocketManager
from pymongo import DESCENDING
from fastapi.responses import FileResponse
from fastapi import FastAPI, File, UploadFile, Form
from fastapi.websockets import WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import boto3
from uuid import uuid4 

app = FastAPI()

# AWS S3 Configuration
AWS_ACCESS_KEY = "AKIAQ75LUUKN35YJFEYR"
AWS_SECRET_KEY = "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"
S3_BUCKET_NAME = "anonymous-app"

s3_client = boto3.client(
    "s3",
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
    region_name="ap-south-1"  
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Your frontend URL
    allow_credentials=True,
    allow_methods=["*"],  # Allow all HTTP methods (GET, POST, etc.)
    allow_headers=["*"],  # Allow all headers
)
socket_manager = SocketManager(app)  # WebSocket instance


# Load environment variables
load_dotenv()
MONGO_URI = os.getenv("MONGO_URI")

# Connect to MongoDB
client = AsyncIOMotorClient(MONGO_URI)
db = client["anonymous_views_db"]
collection = db["views"]

BAD_WORDS = {"hate", "abuse", "toxic"}  # Add more as needed

def contains_bad_words(text):
    return any(word in text.lower() for word in BAD_WORDS)

from fastapi import File, UploadFile
import os
from datetime import datetime

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)  # Create upload directory if not exists

@app.post("/submit")
async def submit_view(
    
    text: str = Form(...),
    file: UploadFile = File(None)
):
    """Submit a view with optional image/video."""
    
    if contains_bad_words(text):
        raise HTTPException(status_code=400, detail="Inappropriate content detected.")

    view = {
        "text": text,
        "timestamp": datetime.utcnow().isoformat(),
        "upvotes": 0,
        "downvotes": 0,
        "comments": [],
        "media_url": []
    }

    file_url = None
    if file:
        file_ext = file.filename.split(".")[-1]
        if file_ext not in ["jpg", "jpeg", "png", "mp4", "webm"]:
            raise HTTPException(status_code=400, detail="Invalid file type")

        unique_filename = f"{uuid4().hex}.{file_ext}"
        s3_client.upload_fileobj(file.file, S3_BUCKET_NAME, unique_filename)

    file_url = f"https://{S3_BUCKET_NAME}.s3.amazonaws.com/{unique_filename}"
    url = await get_media(unique_filename)
    media_url = url["url"]
    view["media_url"].append(media_url)
    result = await collection.insert_one(view)
    view["_id"] = str(result.inserted_id)
    await socket_manager.emit("new_view", view)  # Broadcast to WebSocket clients
    
    return {"message": "View submitted successfully", "id": view["_id"], "media_url": media_url}


@app.post("/upvote/{view_id}")
async def upvote_view(view_id: str):
    result = await collection.update_one(
        {"_id": ObjectId(view_id)},
        {"$inc": {"upvotes": 1}}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="View not found")
    return {"message": "Upvoted successfully"}

@app.post("/comment/{view_id}")
async def add_comment(view_id: str, comment: dict):
    if contains_bad_words(comment.get("text", "")):
        raise HTTPException(status_code=400, detail="Inappropriate comment detected.")

    comment["timestamp"] = datetime.utcnow().isoformat()
    comment["id"] = str(uuid.uuid4())
    comment["upvotes"] = 0

    result = await collection.update_one(
        {"_id": ObjectId(view_id)},
        {"$push": {"comments": comment}}
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="View not found")

    await socket_manager.emit("new_comment", {"view_id": view_id, "comment": comment})
    return {"message": "Comment added successfully", "comment_id": comment["id"]}


@app.post("/comment/upvote/{view_id}/{comment_id}")
async def upvote_comment(view_id: str, comment_id: str):
    """Upvote a comment."""
    result = await collection.update_one(
        {"_id": ObjectId(view_id), "comments._id": ObjectId(comment_id)},
        {"$inc": {"comments.$.upvotes": 1}}  # Increment upvote count
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="View or Comment not found")

    return {"message": "Comment upvoted successfully"}

@app.post("/report/view/{view_id}")
async def report_view(view_id: str, report: dict):
    """Report an inappropriate view."""
    view = await collection.find_one({"_id": ObjectId(view_id)})
    
    if not view:
        raise HTTPException(status_code=404, detail="View not found")
    
    # Add report information to the view
    result = await collection.update_one(
        {"_id": ObjectId(view_id)},
        {"$push": {"reports": report}}  # Push the report to the reports array
    )

    return {"message": "View reported successfully"}

@app.post("/report/comment/{view_id}/{comment_id}")
async def report_comment(view_id: str, comment_id: str, report: dict):
    """Report an inappropriate comment."""
    result = await collection.update_one(
        {"_id": ObjectId(view_id), "comments._id": ObjectId(comment_id)},
        {"$push": {"comments.$.reports": report}}  # Push the report to the comment's reports
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="View or Comment not found")

    return {"message": "Comment reported successfully"}

@app.post("/react/{view_id}")
async def react_to_view(view_id: str, reaction: str):
    """Allows users to like/dislike a view."""
    if reaction not in ["like", "dislike"]:
        raise HTTPException(status_code=400, detail="Invalid reaction")

    update_field = "upvotes" if reaction == "like" else "downvotes"

    result = await collection.update_one(
        {"_id": ObjectId(view_id)},
        {"$inc": {update_field: 1}}
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="View not found")

    return {"message": f"View {reaction}d successfully"}

@app.get("/search")
async def search_views(keyword: str = "", start_date: str = None, end_date: str = None):
    """Search views by keyword and filter by date range."""
    query = {}

    if keyword:
        query["text"] = {"$regex": keyword, "$options": "i"}  # Case-insensitive search

    if start_date and end_date:
        query["timestamp"] = {"$gte": start_date, "$lte": end_date}

    views = await collection.find(query).sort("timestamp", DESCENDING).to_list(length=100)
    return {"views": [serialize_document(view) for view in views]}

@app.get("/views/popular")
async def get_popular_views():
    views = await collection.find().sort("upvotes", -1).to_list(length=100)
    return {"views": [serialize_document(view) for view in views]}

# Utility function to convert MongoDB documents to JSON serializable format
def serialize_document(doc):
    doc["_id"] = str(doc["_id"])  # Convert ObjectId to string
    return doc

# API to get all views
@app.get("/views")
async def get_views(page: int = 1, page_size: int = 10):
    """Fetch views with pagination."""
    skip = (page - 1) * page_size  # Calculate how many to skip
    views = await collection.find().sort("timestamp", -1).skip(skip).limit(page_size).to_list(length=page_size)
    return {"page": page, "views": [serialize_document(view) for view in views]}

@app.get("/views/{view_id}")
async def get_view(view_id: str):
    """Retrieve a specific view along with its comments."""
    view = await collection.find_one({"_id": ObjectId(view_id)})
    if not view:
        raise HTTPException(status_code=404, detail="View not found")
    return serialize_document(view)

S3_REGION = "ap-south-1"
S3_BASE_URL = f"https://{S3_BUCKET_NAME}.s3.{S3_REGION}.amazonaws.com/"
@app.get("/media/{unique_filename}")
async def get_media(unique_filename: str):
    """Generate a pre-signed URL for accessing S3 media."""
    try:
        presigned_url = s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": S3_BUCKET_NAME, "Key": unique_filename},
            ExpiresIn=3600,  # URL expires in 1 hour
        )
        return {"url": presigned_url}
    except NoCredentialsError:
        raise HTTPException(status_code=500, detail="AWS credentials not found")

class ConnectionManager:
    def __init__(self):
        self.active_connections = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    async def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def emit(self, event: str, data: dict):
        for connection in self.active_connections:
            await connection.send_json({"event": event, "data": data})

socket_manager = ConnectionManager()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    while True:
        data = await websocket.receive_text()
        await websocket.send_text(f"Message received: {data}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
