"""F8-2: Upskill pipeline package — teacher-student fine-tuning support.

Modules:
  teachers — `TeacherAdapter` Protocol + `GeminiTeacher` / `ClaudeTeacher`
             implementations + `make_teacher(name)` factory.

The orphan top-level scripts (upskill_pipeline.py, train_upskill.py) import
from this package so the teacher choice is a runtime flag rather than a
hard-coded constant.
"""
