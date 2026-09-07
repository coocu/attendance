from datetime import date, datetime, timezone
from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, BigInteger, String, Text, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column
from db import Base

def utcnow(): return datetime.now(timezone.utc)
class Academy(Base):
    __tablename__="academies"; __table_args__=(UniqueConstraint("region","district","name",name="uq_academy_loc"),Index("ix_acad_loc","is_active","region","district","name"))
    id:Mapped[int]=mapped_column(BigInteger,primary_key=True,autoincrement=True); name:Mapped[str]=mapped_column(String(120),nullable=False); region:Mapped[str]=mapped_column(String(50),nullable=False); district:Mapped[str]=mapped_column(String(80),nullable=False); recovery_name:Mapped[str]=mapped_column(String(80),nullable=False); recovery_phone_last4:Mapped[str]=mapped_column(String(4),nullable=False); is_active:Mapped[bool]=mapped_column(Boolean,default=True,nullable=False); is_24_hours:Mapped[bool]=mapped_column(Boolean,default=True,nullable=False); open_time:Mapped[str]=mapped_column(String(5),default="09:00",nullable=False); close_time:Mapped[str]=mapped_column(String(5),default="20:00",nullable=False); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow,nullable=False)
class AdminCredential(Base):
    __tablename__="academy_admin_credentials"; academy_id:Mapped[int]=mapped_column(ForeignKey("academies.id",ondelete="CASCADE"),primary_key=True); password_hash:Mapped[str]=mapped_column(String(300),nullable=False); updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow,nullable=False)
class Student(Base):
    __tablename__="students"; __table_args__=(Index("ix_students_name_phone","name","phone_last4"),)
    id:Mapped[int]=mapped_column(BigInteger,primary_key=True,autoincrement=True); name:Mapped[str]=mapped_column(String(80),nullable=False); phone_last4:Mapped[str]=mapped_column(String(11),nullable=False); nfc_token:Mapped[str]=mapped_column(String(160),nullable=False,unique=True,index=True); nfc_active:Mapped[bool]=mapped_column(Boolean,default=True,nullable=False); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow,nullable=False); updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow,nullable=False)
class StudentAcademy(Base):
    __tablename__="student_academies"; __table_args__=(UniqueConstraint("student_id","academy_id",name="uq_student_academy"),UniqueConstraint("academy_id","attendance_pin",name="uq_acad_pin"),Index("ix_sa_acad","academy_id","is_active"))
    id:Mapped[int]=mapped_column(BigInteger,primary_key=True,autoincrement=True); student_id:Mapped[int]=mapped_column(ForeignKey("students.id",ondelete="CASCADE"),nullable=False,index=True); academy_id:Mapped[int]=mapped_column(ForeignKey("academies.id",ondelete="CASCADE"),nullable=False,index=True); attendance_pin:Mapped[str]=mapped_column(String(4),nullable=False); memo:Mapped[str]=mapped_column(Text,default="",nullable=False); login_extra_code:Mapped[str|None]=mapped_column(String(6),nullable=True); is_active:Mapped[bool]=mapped_column(Boolean,default=True,nullable=False); withdrawn_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow,nullable=False)
class AttendanceEvent(Base):
    __tablename__="attendance_events"; __table_args__=(Index("ix_att_st","student_id","occurred_at"),Index("ix_att_ac","academy_id","occurred_at"))
    id:Mapped[int]=mapped_column(BigInteger,primary_key=True,autoincrement=True); academy_id:Mapped[int]=mapped_column(ForeignKey("academies.id",ondelete="CASCADE"),nullable=False); student_id:Mapped[int]=mapped_column(ForeignKey("students.id",ondelete="CASCADE"),nullable=False); student_academy_id:Mapped[int]=mapped_column(ForeignKey("student_academies.id",ondelete="CASCADE"),nullable=False); event_type:Mapped[str]=mapped_column(String(3),nullable=False); source:Mapped[str]=mapped_column(String(20),nullable=False); occurred_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow,nullable=False,index=True)
class ParentDevice(Base):
    __tablename__="parent_devices"; __table_args__=(UniqueConstraint("installation_id","platform",name="uq_install_platform"),)
    id:Mapped[int]=mapped_column(BigInteger,primary_key=True,autoincrement=True); installation_id:Mapped[str]=mapped_column(String(120),nullable=False); platform:Mapped[str]=mapped_column(String(20),nullable=False); push_token:Mapped[str|None]=mapped_column(String(512)); updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow,nullable=False)
class AdminDevice(Base):
    __tablename__="admin_devices"; __table_args__=(UniqueConstraint("academy_id","installation_id","platform",name="uq_admin_device"),Index("ix_admin_device_alert","academy_id","attendance_alert_enabled"))
    id:Mapped[int]=mapped_column(BigInteger,primary_key=True,autoincrement=True); academy_id:Mapped[int]=mapped_column(ForeignKey("academies.id",ondelete="CASCADE"),nullable=False,index=True); installation_id:Mapped[str]=mapped_column(String(120),nullable=False); platform:Mapped[str]=mapped_column(String(20),nullable=False); push_token:Mapped[str|None]=mapped_column(String(512)); attendance_alert_enabled:Mapped[bool]=mapped_column(Boolean,default=False,nullable=False); updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow,nullable=False)
class ParentLink(Base):
    __tablename__="parent_links"; __table_args__=(UniqueConstraint("device_id","student_id","academy_id",name="uq_parent_link"),)
    id:Mapped[int]=mapped_column(BigInteger,primary_key=True,autoincrement=True); device_id:Mapped[int]=mapped_column(ForeignKey("parent_devices.id",ondelete="CASCADE"),nullable=False); student_id:Mapped[int]=mapped_column(ForeignKey("students.id",ondelete="CASCADE"),nullable=False,index=True); academy_id:Mapped[int]=mapped_column(ForeignKey("academies.id",ondelete="CASCADE"),nullable=False); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow,nullable=False)
class Notice(Base):
    __tablename__="notices"; notice_type:Mapped[str]=mapped_column(String(20),primary_key=True); content:Mapped[str]=mapped_column(Text,default="",nullable=False); is_active:Mapped[bool]=mapped_column(Boolean,default=False,nullable=False); updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow,nullable=False)

class AcademyNotice(Base):
    __tablename__="academy_notices"; __table_args__=(UniqueConstraint("academy_id","notice_type",name="uq_academy_notice_type"),Index("ix_academy_notice_active","academy_id","is_active"))
    id:Mapped[int]=mapped_column(BigInteger,primary_key=True,autoincrement=True); academy_id:Mapped[int]=mapped_column(ForeignKey("academies.id",ondelete="CASCADE"),nullable=False,index=True); notice_type:Mapped[str]=mapped_column(String(20),nullable=False); content:Mapped[str]=mapped_column(Text,default="",nullable=False); is_active:Mapped[bool]=mapped_column(Boolean,default=False,nullable=False); start_date:Mapped[date|None]=mapped_column(Date,nullable=True); end_date:Mapped[date|None]=mapped_column(Date,nullable=True); updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow,nullable=False)
class AcademyNoticeTemplate(Base):
    __tablename__="academy_notice_templates"; __table_args__=(UniqueConstraint("academy_id","slot",name="uq_academy_notice_template_slot"),)
    id:Mapped[int]=mapped_column(BigInteger,primary_key=True,autoincrement=True); academy_id:Mapped[int]=mapped_column(ForeignKey("academies.id",ondelete="CASCADE"),nullable=False,index=True); slot:Mapped[int]=mapped_column(Integer,nullable=False); content:Mapped[str]=mapped_column(Text,default="",nullable=False); updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow,nullable=False)

class AcademyAdminNotice(Base):
    __tablename__="academy_admin_notices"
    __table_args__=(
        UniqueConstraint("academy_id","notice_type",name="uq_academy_admin_notice_type"),
        Index("ix_academy_admin_notice_active","academy_id","is_active"),
    )
    id:Mapped[int]=mapped_column(BigInteger,primary_key=True,autoincrement=True)
    academy_id:Mapped[int]=mapped_column(ForeignKey("academies.id",ondelete="CASCADE"),nullable=False,index=True)
    notice_type:Mapped[str]=mapped_column(String(20),nullable=False)
    content:Mapped[str]=mapped_column(Text,default="",nullable=False)
    is_active:Mapped[bool]=mapped_column(Boolean,default=False,nullable=False)
    updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow,nullable=False)

class AcademySchedule(Base):
    __tablename__="academy_schedules"
    __table_args__=(
        Index("ix_academy_schedule_range","academy_id","start_date","end_date"),
    )
    id:Mapped[int]=mapped_column(BigInteger,primary_key=True,autoincrement=True)
    academy_id:Mapped[int]=mapped_column(ForeignKey("academies.id",ondelete="CASCADE"),nullable=False,index=True)
    kind:Mapped[str]=mapped_column(String(20),nullable=False)
    start_date:Mapped[date]=mapped_column(Date,nullable=False)
    end_date:Mapped[date]=mapped_column(Date,nullable=False)
    title:Mapped[str]=mapped_column(String(160),default="",nullable=False)
    content:Mapped[str]=mapped_column(Text,default="",nullable=False)
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow,nullable=False)
    updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow,nullable=False)

class AcademyScheduleSetting(Base):
    __tablename__="academy_schedule_settings"
    __table_args__=(
        UniqueConstraint("academy_id","scope","year_month",name="uq_academy_schedule_setting"),
    )
    id:Mapped[int]=mapped_column(BigInteger,primary_key=True,autoincrement=True)
    academy_id:Mapped[int]=mapped_column(ForeignKey("academies.id",ondelete="CASCADE"),nullable=False,index=True)
    scope:Mapped[str]=mapped_column(String(20),nullable=False)
    year_month:Mapped[str]=mapped_column(String(7),default="",nullable=False)
    weekdays:Mapped[str]=mapped_column(String(20),default="",nullable=False)
    holiday_auto:Mapped[bool]=mapped_column(Boolean,default=False,nullable=False)
    updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow,nullable=False)

class AcademyScheduleException(Base):
    __tablename__="academy_schedule_exceptions"
    __table_args__=(
        UniqueConstraint("academy_id","date",name="uq_academy_schedule_exception"),
        Index("ix_academy_schedule_exception_date","academy_id","date"),
    )
    id:Mapped[int]=mapped_column(BigInteger,primary_key=True,autoincrement=True)
    academy_id:Mapped[int]=mapped_column(ForeignKey("academies.id",ondelete="CASCADE"),nullable=False,index=True)
    date:Mapped[date]=mapped_column(Date,nullable=False)
    is_closed:Mapped[bool]=mapped_column(Boolean,nullable=False)
    updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow,nullable=False)

class AcademyScheduleRuleHistory(Base):
    __tablename__="academy_schedule_rule_history"
    __table_args__=(
        UniqueConstraint("academy_id","scope","year_month","effective_from",name="uq_academy_schedule_rule_history"),
        Index("ix_academy_schedule_rule_history_lookup","academy_id","scope","year_month","effective_from"),
    )
    id:Mapped[int]=mapped_column(BigInteger,primary_key=True,autoincrement=True)
    academy_id:Mapped[int]=mapped_column(ForeignKey("academies.id",ondelete="CASCADE"),nullable=False,index=True)
    scope:Mapped[str]=mapped_column(String(20),nullable=False)
    year_month:Mapped[str]=mapped_column(String(7),default="",nullable=False)
    effective_from:Mapped[date]=mapped_column(Date,nullable=False)
    weekdays:Mapped[str]=mapped_column(String(20),default="",nullable=False)
    holiday_auto:Mapped[bool]=mapped_column(Boolean,default=False,nullable=False)
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow,nullable=False)
