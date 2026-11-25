---
tags: [springboot-startup-tuning]
max_turns: 40
timeout_seconds: 1500
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

แอป Spring Boot ตัวนี้ start ช้า ช่วย review แล้ววัดดูหน่อยว่าอะไรกินเวลา แล้วลองปรับ config ให้ start เร็วขึ้น เอาเฉพาะที่วัดแล้วช่วยจริง สรุปเป็นรายงาน before/after ให้ด้วย โปรเจกต์อยู่ที่ demo-app/ (ใช้ H2 in-memory ไม่ต้องมี DB ข้างนอก)
Input files: demo-app
