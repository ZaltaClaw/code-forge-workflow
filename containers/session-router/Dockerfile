# =============================================================================
# Session Router — claims/releases agent pods on behalf of developers.
# =============================================================================
FROM golang:1.22-bookworm AS build
WORKDIR /src
COPY go.mod ./
RUN go mod download || true
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags='-s -w' -o /out/router ./...

FROM gcr.io/distroless/static:nonroot
USER nonroot:nonroot
COPY --from=build /out/router /router
EXPOSE 8080
ENTRYPOINT ["/router"]
