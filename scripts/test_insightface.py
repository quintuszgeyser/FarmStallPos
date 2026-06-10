"""Quick test to figure out insightface 0.2.1 API"""
import insightface
import pprint

print("InsightFace version:", insightface.__version__)
print("\nAvailable in insightface.app:")
pprint.pprint(dir(insightface.app))

print("\n\nAvailable in insightface.model_zoo:")
pprint.pprint(dir(insightface.model_zoo))

print("\n\nTrying to list available models:")
try:
    from insightface.model_zoo import model_zoo
    print("model_zoo.onnx_models:", model_zoo.onnx_models if hasattr(model_zoo, 'onnx_models') else 'N/A')
    print("model_zoo.model_list:", model_zoo.model_list if hasattr(model_zoo, 'model_list') else 'N/A')
except Exception as e:
    print("Error:", e)

print("\n\nTrying FaceAnalysis init signatures:")
import inspect
try:
    sig = inspect.signature(insightface.app.FaceAnalysis.__init__)
    print("FaceAnalysis.__init__ signature:", sig)
except Exception as e:
    print("Error:", e)
