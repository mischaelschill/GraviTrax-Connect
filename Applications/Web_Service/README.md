# GraviTrax Connect Web Service

This is a RESTful API web service for interacting with GraviTrax Connect stones. It provides HTTP endpoints to connect to a bridge, send signals, and receive notifications through a web interface.

## Installation

1. Make sure you have Python 3.10 or newer installed.
2. Install the required dependencies:
   ```
   pip install -r requirements.txt
   ```

## Usage

### Starting the Web Service

Run the following command to start the web service:

```
python gravitrax_web_service.py
```

The service will start on port 5000 by default. You can access it at `http://localhost:5000`.

### API Endpoints

See [openapi.yaml](openapi.yaml).

## Example Usage

### Connect to Bridge

```bash
curl -X POST http://localhost:5000/api/v1/connect
```

### Send a Red Signal

```bash
curl -X POST http://localhost:5000/api/v1/signal \
  -H "Content-Type: application/json" \
  -d '{"color": "red", "status": "ALL", "stone": "bridge"}'
```

### Get Notifications

```bash
curl http://localhost:5000/api/v1/notifications
```

### List Available Bridges (scan)

```bash
# Scan for 5 seconds (default) and return discovered MAC addresses
curl "http://localhost:5000/api/v1/bridges"

# Scan for 10 seconds
curl "http://localhost:5000/api/v1/bridges?timeout=10"

# Scan with a custom name filter (use name="" to scan all BLE devices)
curl "http://localhost:5000/api/v1/bridges?name=GraviTrax%20Bridge"
```

## Integration with Web Applications

This web service can be used as a backend for web applications that need to interact with GraviTrax Connect stones. The API is designed to be RESTful and easy to integrate with frontend frameworks like React, Angular, or Vue.js.

CORS is enabled for all routes, so you can make requests from any origin.

## OpenAPI Specification

An OpenAPI 3.0 specification for this web service is available at:

- `Applications/Web_Service/openapi.yaml`

You can view it using any OpenAPI viewer such as:
- https://editor.swagger.io/ (File -> Import File and select `openapi.yaml`)
- Redocly CLI or Swagger UI locally

This specification documents all endpoints, request/response schemas, and examples for quick integration.

## API Versioning

- The API is now versioned under the /api/v1/ prefix (e.g., /api/v1/status).
- For backward compatibility, the previous unversioned routes under /api/... still work for now, but new integrations should use /api/v1/.
