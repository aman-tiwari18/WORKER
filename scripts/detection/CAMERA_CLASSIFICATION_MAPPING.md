# Camera Classification Mapping for Analytics

This document defines the standardized camera classification keywords and their mapping to detection analytics.

## Standardized Classification Keywords

The system recognizes the following camera classification types:

### 1. **Computer Lab / Inside Lab / Exam Lab**
- Variants: `computer lab`, `inside lab`, `exam lab`, `lab`, `inside classroom`, `classroom`
- Description: Indoor examination halls, computer labs, classrooms where candidates take exams

### 2. **Server Room**
- Variants: `server room`, `server_room`, `serverroom`, `inside server room`
- Description: Server rooms, strong rooms, secure areas with equipment

### 3. **Entry / Entry-Exit**
- Variants: `entry`, `entry/exit`, `entry_exit`, `entryexit`, `entry gate`, `exit gate`
- Description: Entry and exit points, gates, doorways

### 4. **Outdoor Scene / Outside Area / Biometric Area**
- Variants: `outdoor scene`, `outdoor_scene`, `outside area`, `outside_area`, `biometric area`, `biometric_area`, `biometric`
- Description: Outdoor areas, biometric verification zones, parking lots

### 5. **Parking Lot**
- Variants: `parking lot`, `parking_lot`, `parking`
- Description: Vehicle parking areas

### 6. **Uncertain/Obstructed**
- Variants: `uncertain`, `obstructed`, `uncertain/obstructed`
- Description: Cameras with unclear or obstructed views

---

## Analytics to Camera Type Mapping

| Sr. No. | Analytics | Frequency | Camera Classification | Script Code |
|---------|-----------|-----------|----------------------|-------------|
| 1 | Intruder detection in exam lab and server room | - | Inside Lab, Server Room | `intruder` |
| 2 | Improper furniture | - | Inside Lab | - |
| 3 | Crowd Detection - More than 10 people at entry gate | 2 Minutes | Entry/Exit, Outside/Biometric Area | `crowd` |
| 4 | Invigilator Not Moving in exam room or not moving in all lanes | 7 Minutes | Inside Lab | `invigilator` |
| 5 | Two or More People in Close Proximity | 2 Minutes | Inside Lab | `proximity` |
| 6 | Server Room Threshold - More than or equal to the limit | 2 Minutes | Server Room | `server` |
| 7 | Mobile Phone Detection | 1.5 Minutes (Lab)<br>7 Minutes (Server) | Inside Lab, Inside Server Room | `phone` |
| 8 | Movement at entry/exit during examination | 2 Minutes | Outside (Entry/Exit Gate) | `entry_exit` |
| 9 | Camera feed is inverted | - | All Types | `inversion` |
| 10 | 0 candidate in a room | - | Inside Lab | `crowd_zero` |
| 11 | Less than 5 candidates in a room | - | Inside Lab | `crowd_five` |
| 12 | Blind Spot detected inside lab | - | Inside Lab | `blind_spot` |

---

## Worker Scene Filtering Logic

Each worker implements scene filtering to process only relevant camera types:

### intruder_worker.py
```python
# Filters: Inside Lab, Server Room
valid = any(s in classification for s in (
    'computer lab', 'inside lab', 'exam lab', 'lab',
    'server room', 'server_room', 'serverroom'
))
```

### crowd_worker.py
```python
# Filters: Entry/Exit, Outside/Biometric Area
valid = any(s in classification for s in (
    'entry', 'entry/exit', 'entry_exit', 'entryexit',
    'outdoor scene', 'outdoor_scene', 'outside area', 'outside_area',
    'biometric area', 'biometric_area', 'biometric'
))
```

### invigilator_worker.py
```python
# Filters: Inside Lab
valid = any(s in classification for s in (
    'computer lab', 'inside lab', 'exam lab', 'lab',
    'inside classroom', 'inside_classroom', 'classroom'
))
```

### close_proximity_worker.py
```python
# Filters: Inside Lab
valid = any(s in classification for s in (
    'computer lab', 'inside lab', 'exam lab', 'lab',
    'inside classroom', 'inside_classroom', 'classroom'
))
```

### phone_worker.py
```python
# Filters: Inside Lab, Inside Server Room
valid = any(s in classification for s in (
    'computer lab', 'inside lab', 'exam lab', 'lab',
    'inside classroom', 'inside_classroom', 'classroom',
    'server room', 'server_room', 'serverroom', 'inside server room'
))
```

### entry_exit_worker.py
```python
# Filters: Entry/Exit, Outside
valid = any(s in classification for s in (
    'entry', 'entry/exit', 'entry_exit', 'entryexit',
    'outdoor scene', 'outdoor_scene', 'outside area', 'outside_area',
    'crowd at entry/exit gate', 'entry gate', 'exit gate'
))
```

### crowd_zero_det_worker.py
```python
# Filters: Inside Lab (0 candidates)
valid = any(s in classification for s in (
    'computer lab', 'inside lab', 'exam lab', 'lab',
    'inside classroom', 'inside_classroom', 'classroom'
))
```

### crowd_five_det_worker.py
```python
# Filters: Inside Lab (less than 5 candidates)
valid = any(s in classification for s in (
    'computer lab', 'inside lab', 'exam lab', 'lab',
    'inside classroom', 'inside_classroom', 'classroom'
))
```

### blind_spot_worker.py
```python
# Filters: Inside Lab
valid = any(s in classification for s in (
    'computer lab', 'inside lab', 'exam lab', 'lab',
    'inside classroom', 'inside_classroom', 'classroom'
))
```

### camera_inversion_worker.py
```python
# No filtering - applies to all camera types
# Camera inversion can happen on any camera
```

---

## Usage Guidelines

### For Backend/Database
When storing camera configurations, use one of the standardized classification keywords:
- `Computer Lab`
- `Server Room`
- `Entry`
- `Outdoor Scene`
- `Parking Lot`
- `Uncertain/Obstructed`

### For Workers
Workers automatically filter cameras based on the `classification` field in the camera config:
```python
cfg = {
    'camera_id': '12345',
    'classification': 'Computer Lab',  # ← This field determines which analytics run
    'cctv_url': 'rtsp://...',
    # ... other fields
}
```

### Empty Classification
If `classification` is empty or missing, the camera will be processed by **all** analytics (no filtering applied).

---

## Testing

To test classification filtering:

1. Set camera classification in database
2. Assign analytics to the camera
3. Verify only appropriate workers process the camera
4. Check worker logs for "Scene filtering" messages

Example log output:
```
[Intruder] Scene filtering: 15 cameras → 8 valid (Inside Lab/Server Room)
[Crowd] Scene filtering: 15 cameras → 5 valid (Entry/Exit/Outside)
```

---

## Migration Notes

If you have existing cameras with old classification values:
- `Strong Room` → Update to `Server Room`
- `Inside Classroom` → Update to `Computer Lab` or `Inside Lab`
- `Entry/Exit` → Keep as-is (already standardized)
- `Outside/Biometric Area` → Update to `Outdoor Scene` or `Outside Area`

The workers support legacy variants for backward compatibility, but new cameras should use standardized keywords.
