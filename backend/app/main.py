from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import seed
from app.modules.在途超时作废 import router as 超时router
from app.modules.在途超时作废 import 确保结构
from app.routers import api

app = FastAPI(title="影湾", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
def _启动():
    seed.初始化库()
    确保结构()


app.include_router(api)
app.include_router(超时router)
