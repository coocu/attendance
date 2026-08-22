from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, BigInteger, String, Text, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column
from db import Base

def utcnow(): return datetime.now(timezone.utc)
class Academy(Base):
    __tablename__="academies"; __table_args__=(UniqueConstraint("region","district","name",name="uq_academy_loc"),Index("ix_acad_loc","is_active","region","district","name"))
    id:Mapped[int]=mapped_column(BigInteger,primary_key=True,autoincrement=True); name:Mapped[str]=mapped_column(String(120),nullable=False); region:Mapped[str]=mapped_column(String(50),nullable=False); district:Mapped[str]=mapped_column(String(80),nullable=False); recovery_name:Mapped[str]=mapped_column(String(80),nullable=False); recovery_phone_last4:Mapped[str]=mapped_column(String(4),nullable=False); is_active:Mapped[bool]=mapped_column(Boolean,default=True,nullable=False); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow,nullable=False)
class AdminCredential(Base):
    __tablename__="academy_admin_credentials"; academy_id:Mapped[int]=mapped_column(ForeignKey("academies.id",ondelete="CASCADE"),primary_key=True); password_hash:Mapped[str]=mapped_column(String(300),nullable=False); updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow,nullable=False)
class Student(Base):
    __tablename__="students"; __table_args__=(Index("ix_students_name_phone","name","phone_last4"),)
    id:Mapped[int]=mapped_column(BigInteger,primary_key=True,autoincrement=True); name:Mapped[str]=mapped_column(String(80),nullable=False); phone_last4:Mapped[str]=mapped_column(String(11),nullable=False); nfc_token:Mapped[str]=mapped_column(String(160),nullable=False,unique=True,index=True); nfc_active:Mapped[bool]=mapped_column(Boolean,default=True,nullable=False); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow,nullable=False); updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow,nullable=False)
class StudentAcademy(Base):
    __tablename__="student_academies"; __table_args__=(UniqueConstraint("student_id","academy_id",name="uq_student_academy"),UniqueConstraint("academy_id","attendance_pin",name="uq_acad_pin"),Index("ix_sa_acad","academy_id","is_active"))
    id:Mapped[int]=mapped_column(BigInteger,primary_key=True,autoincrement=True); student_id:Mapped[int]=mapped_column(ForeignKey("students.id",ondelete="CASCADE"),nullable=False,index=True); academy_id:Mapped[int]=mapped_column(ForeignKey("academies.id",ondelete="CASCADE"),nullable=False,index=True); attendance_pin:Mapped[str]=mapped_column(String(4),nullable=False); memo:Mapped[str]=mapped_column(Text,default="",nullable=False); login_extra_code:Mapped[str|None]=mapped_column(String(6),nullable=True); is_active:Mapped[bool]=mapped_column(Boolean,default=True,nullable=False); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow,nullable=False)
class AttendanceEvent(Base):
    __tablename__="attendance_events"; __table_args__=(Index("ix_att_st","student_id","occurred_at"),Index("ix_att_ac","academy_id","occurred_at"))
    id:Mapped[int]=mapped_column(BigInteger,primary_key=True,autoincrement=True); academy_id:Mapped[int]=mapped_column(ForeignKey("academies.id",ondelete="CASCADE"),nullable=False); student_id:Mapped[int]=mapped_column(ForeignKey("students.id",ondelete="CASCADE"),nullable=False); student_academy_id:Mapped[int]=mapped_column(ForeignKey("student_academies.id",ondelete="CASCADE"),nullable=False); event_type:Mapped[str]=mapped_column(String(3),nullable=False); source:Mapped[str]=mapped_column(String(20),nullable=False); occurred_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow,nullable=False,index=True)
class ParentDevice(Base):
    __tablename__="parent_devices"; __table_args__=(UniqueConstraint("installation_id","platform",name="uq_install_platform"),)
    id:Mapped[int]=mapped_column(BigInteger,primary_key=True,autoincrement=True); installation_id:Mapped[str]=mapped_column(String(120),nullable=False); platform:Mapped[str]=mapped_column(String(20),nullable=False); push_token:Mapped[str|None]=mapped_column(String(512)); updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow,nullable=False)
class ParentLink(Base):
    __tablename__="parent_links"; __table_args__=(UniqueConstraint("device_id","student_id","academy_id",name="uq_parent_link"),)
    id:Mapped[int]=mapped_column(BigInteger,primary_key=True,autoincrement=True); device_id:Mapped[int]=mapped_column(ForeignKey("parent_devices.id",ondelete="CASCADE"),nullable=False); student_id:Mapped[int]=mapped_column(ForeignKey("students.id",ondelete="CASCADE"),nullable=False,index=True); academy_id:Mapped[int]=mapped_column(ForeignKey("academies.id",ondelete="CASCADE"),nullable=False); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow,nullable=False)
class Notice(Base):
    __tablename__="notices"; notice_type:Mapped[str]=mapped_column(String(20),primary_key=True); content:Mapped[str]=mapped_column(Text,default="",nullable=False); is_active:Mapped[bool]=mapped_column(Boolean,default=False,nullable=False); updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow,nullable=False)
