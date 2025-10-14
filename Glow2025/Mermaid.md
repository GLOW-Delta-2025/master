flowchart TD

A([Start]) --> B[Mic Input Detected]
B --> C[Trigger MAKE_STAR<br>→ Star lights up]
C --> D[Mic Input Stops]
D --> E[Trigger SEND_STAR<br>→ Star travels through arm]
E --> F[Arm Sends "Star Arrived" Signal to Mac]
F --> G[Mac Triggers ADD_STAR_CENTER<br>→ Increment Star Counter +1]
G --> H{Star Counter = 5?}

H -->|No| B
H -->|Yes| I[Trigger BUILD_UP_START<br>(10 seconds)]

I --> J[After 10s → Send CLIMAX_READY<br>from Center to Mac]
J --> K[Trigger START_CLIMAX_TOP<br>and START_CLIMAX_CENTER]
K --> L[Wait 15 seconds]
L --> M[Reset Star Counter to 0]
M --> N([Return to Beginning])

%% --- Styling (optional for GitHub light/dark mode) ---
style A fill:#c6f6d5,stroke:#2f855a,stroke-width:2px
style N fill:#c6f6d5,stroke:#2f855a,stroke-width:2px
style B fill:#bee3f8,stroke:#2b6cb0,stroke-width:1px
style C fill:#bee3f8,stroke:#2b6cb0,stroke-width:1px
style D fill:#bee3f8,stroke:#2b6cb0,stroke-width:1px
style E fill:#bee3f8,stroke:#2b6cb0,stroke-width:1px
style F fill:#bee3f8,stroke:#2b6cb0,stroke-width:1px
style G fill:#faf089,stroke:#b7791f,stroke-width:1px
style H fill:#fbd38d,stroke:#b7791f,stroke-width:2px
style I fill:#feb2b2,stroke:#c53030,stroke-width:1px
style J fill:#fed7d7,stroke:#c53030,stroke-width:1px
style K fill:#fbb6ce,stroke:#97266d,stroke-width:1px
style L fill:#fbb6ce,stroke:#97266d,stroke-width:1px
style M fill:#faf089,stroke:#b7791f,stroke-width:1px
