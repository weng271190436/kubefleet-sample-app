# KubeFleet Sample App

A sample Python + React application for demonstrating KubeFleet multi-cluster deployment.

## Architecture

- **Backend**: FastAPI (Python) — CRUD API for configuration items
- **Frontend**: React + MUI X DataGrid — editable grid UI
- **K8s**: Deployment manifests for both services + namespace

## Local Development

### Backend
```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

### Frontend
```bash
cd frontend
npm install
REACT_APP_API_URL=http://localhost:8000 npm start
```

## Docker Build
```bash
docker build -t sample-backend ./backend
docker build -t sample-frontend ./frontend
```

## Kubernetes Deploy
```bash
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/backend.yaml -n kubefleet-sample
kubectl apply -f k8s/frontend.yaml -n kubefleet-sample
```

## Future Plans
- Add PostgreSQL database backend
- Persistent storage via PVC
- Database migration support
