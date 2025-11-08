from EOTMain import MacMiniController
import threading

# Initialize shared controller
controller = MacMiniController()
threading.Thread(target=controller.run, daemon=True).start()