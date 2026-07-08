from sqlalchemy import Column, String, Integer, Float, ForeignKey, DateTime
from sqlalchemy.orm import declarative_base, relationship
from datetime import datetime

Base = declarative_base()

class FileEntry(Base):
    __tablename__ = "files"
    id = Column(String, primary_key=True)
    filename = Column(String, nullable=False)
    total_size = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    fragments = relationship("FragmentEntry", back_populates="file")

class FragmentEntry(Base):
    __tablename__ = "fragments"
    id = Column(String, primary_key=True)
    file_id = Column(String, ForeignKey("files.id"))
    provider = Column(String, nullable=False)
    name = Column(String, nullable=False)
    size = Column(Integer, nullable=False)
    checksum = Column(String)
    file = relationship("FileEntry", back_populates="fragments")
