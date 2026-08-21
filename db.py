import os
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
DATABASE_URL=os.getenv("DATABASE_URL","postgresql+psycopg://postgres:postgres@localhost:5432/codenote_attendance")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL="postgresql+psycopg://"+DATABASE_URL[len("postgres://"):]
elif DATABASE_URL.startswith("postgresql://") and "+psycopg" not in DATABASE_URL:
    DATABASE_URL="postgresql+psycopg://"+DATABASE_URL[len("postgresql://"):]
engine=create_engine(DATABASE_URL,pool_pre_ping=True)
SessionLocal=sessionmaker(bind=engine,autoflush=False,autocommit=False)
class Base(DeclarativeBase): pass
def get_db():
    db=SessionLocal()
    try: yield db
    finally: db.close()
