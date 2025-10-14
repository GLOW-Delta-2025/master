flowchart TD

A([Start]) --> B[Mic Input Detected]
B --> C[Trigger MAKE_STAR → Star lights up]
C --> D[Mic Input Stops]
D --> E[Trigger SEND_STAR → Star travels through arm]
E --> F[Arm Sends "Star Arrived" Signal to Mac]
F --> G[Mac Triggers ADD_STAR_CENTER → Increment Star Counter +1]
G --> H{Star Counter = 5?}

H -->|No| B
H -->|Yes| I[Trigger BUILD_UP_START (10 seconds)]

I --> J[After 10s → Send CLIMAX_READY from Center to Mac]
J --> K[Trigger START_CLIMAX_TOP and START_CLIMAX_CENTER]
K --> L[Wait 15 seconds]
L --> M[Reset Star Counter to 0]
M --> N([Return to Beginning])
