import os

from kfp import dsl

BASE_IMAGE = os.environ["INGESTION_PIPELINE_IMAGE"]


@dsl.component(base_image=BASE_IMAGE)
def fetch_from_s3(output_dir: dsl.OutputPath()):
    import os
    import boto3

    # S3 Config
    bucket_name = os.environ.get('BUCKET_NAME')
    minio_endpoint = os.environ.get('ENDPOINT_URL')
    minio_access_key = os.environ.get('ACCESS_KEY_ID')
    minio_secret_key = os.environ.get('SECRET_ACCESS_KEY')

    # Ensure the output directory exists
    os.makedirs(output_dir, exist_ok=True)
    print(f"Created output directory: {output_dir}")

    # Connect to MinIO
    print(f"Connecting to MinIO at {minio_endpoint}")
    s3 = boto3.client(
        "s3",
        endpoint_url=minio_endpoint,
        aws_access_key_id=minio_access_key,
        aws_secret_access_key=minio_secret_key,
        verify=False
    )

    # List and download objects
    paginator = s3.get_paginator("list_objects_v2")
    pages = paginator.paginate(Bucket=bucket_name)

    print(f"Downloading files from bucket: {bucket_name}")
    downloaded_files = []
    for page in pages:
        for obj in page.get("Contents", []):
            key = obj["Key"]
            file_path = os.path.join(output_dir, os.path.basename(key))
            print(f"Downloading: {key} -> {file_path}")
            s3.download_file(bucket_name, key, file_path)
            downloaded_files.append(file_path)

    print(f"Downloaded {len(downloaded_files)} files to {output_dir}")

    if not downloaded_files:
        raise Exception(f"No files found in bucket: {bucket_name}. Please check your bucket configuration.")

    print(f"Contents of output directory: {os.listdir(output_dir)}")


@dsl.component(base_image=BASE_IMAGE)
def fetch_from_urls(output_dir: dsl.OutputPath()):
    print(f"Storing documents will fetch from URLS env var")


@dsl.component(base_image=BASE_IMAGE)
def fetch_from_github(output_dir: dsl.OutputPath()):
    import os
    import shutil
    import tempfile

    import git
    os.makedirs(output_dir, exist_ok=True)
    token = os.getenv("GIT_TOKEN")
    url = os.getenv("GIT_URL")
    if token:
        if url.startswith("https://"):
            url = url.replace("https://", f"https://{token}@")
        else:
            raise ValueError("Only HTTPS URLs support token authentication")
    with tempfile.TemporaryDirectory() as tmp_dir:
        kwargs = {"depth": 1, "single_branch": True}
        if branch := os.getenv("GIT_BRANCH"):
            kwargs["branch"] = branch
        git.Repo.clone_from(url, tmp_dir, **kwargs)
        src_dir = os.path.join(tmp_dir, os.getenv("GIT_PATH"))
        if os.path.isdir(src_dir):
            for entry in os.scandir(src_dir):
                if entry.is_file():
                    print(f"Copying {entry.path} to {output_dir}")
                    shutil.copy2(entry.path, os.path.join(output_dir, entry.name))
        else:
            raise RuntimeError(f"Directory {src_dir} not found in the repo.")


@dsl.component(base_image=BASE_IMAGE)
def store_documents(llamastack_base_url: str, input_dir: dsl.InputPath()):
    import os
    from pathlib import Path

    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions

    # Import docling libraries
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling_core.transforms.chunker.hybrid_chunker import HybridChunker
    from docling_core.types.doc.labels import DocItemLabel
    from llama_stack_client import LlamaStackClient
    from llama_stack_client.types import Document as LlamaStackDocument

    os.environ["EASYOCR_MODULE_PATH"] = "/tmp/.EasyOCR"

    # Configuring the vector database
    embedding_model = os.getenv('EMBEDDING_MODEL')
    vector_db_name = os.getenv('VECTOR_DB_NAME')

    # Setup docling components
    pipeline_options = PdfPipelineOptions()
    pipeline_options.generate_picture_images = True
    converter = DocumentConverter(
        allowed_formats=[
            InputFormat.PDF,
            InputFormat.MD,
            InputFormat.DOCX,
            InputFormat.ASCIIDOC,
            InputFormat.JSON_DOCLING,
            InputFormat.HTML
        ],
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
        }
    )
    chunker = HybridChunker()
    llama_documents = []
    i = 0
    # Process each file with docling (chunking)
    input_files = []
    if os.getenv("URLS"):
        import ast
        input_files = ast.literal_eval(os.getenv("URLS","[]"))
    else:
        input_files = [str(p) for p in Path(input_dir).iterdir() if p.is_file()]
    if not input_files:
        raise RuntimeError("No input files found")
    print(f"Input files: {input_files}")
    for file_path in input_files:
        print(f"Processing {file_path} with docling...")
        try:
            docling_doc = converter.convert(source=file_path).document
            chunks = chunker.chunk(docling_doc)
            chunk_count = 0

            for chunk in chunks:
                if any(
                    c.label in [DocItemLabel.TEXT, DocItemLabel.PARAGRAPH, DocItemLabel.TABLE,
                               DocItemLabel.PAGE_HEADER, DocItemLabel.PAGE_FOOTER,
                               DocItemLabel.TITLE, DocItemLabel.PICTURE, DocItemLabel.CHART,
                               DocItemLabel.DOCUMENT_INDEX, DocItemLabel.SECTION_HEADER]
                    for c in chunk.meta.doc_items
                ):
                    i += 1
                    chunk_count += 1
                    llama_documents.append(
                        LlamaStackDocument(
                            document_id=f"doc-{i}",
                            content=chunk.text,
                            mime_type='text/plain',
                            metadata={"source": os.path.basename(file_path)},
                        )
                    )
            print(f"Created {chunk_count} chunks from {file_path}")

        except Exception as e:
            error_message = str(e)
            print(f"Error processing {file_path}: {error_message}")

    total_chunks = len(llama_documents)
    print(f"Total valid chunks prepared: {total_chunks}")

    # Add error handling for zero chunks
    if total_chunks == 0:
        raise Exception("No valid chunks were created. Check document processing errors above.")

    # Step 3: Register vector database and store chunks with embeddings
    client = LlamaStackClient(base_url=llamastack_base_url)
    print("Registering db")
    try:
        client.vector_dbs.register(
            vector_db_id=vector_db_name,
            embedding_model=embedding_model,
            embedding_dimension=384,
            provider_id="pgvector",
        )
        print("Vector DB registered successfully")
    except Exception as e:
        error_message = str(e)
        print(f"Failed to register vector DB: {error_message}")
        raise Exception(f"Vector DB registration failed: {error_message}")

    try:
        print(f"Inserting {total_chunks} chunks into vector database")
        client.tool_runtime.rag_tool.insert(
            documents=llama_documents,
            vector_db_id=vector_db_name,
            chunk_size_in_tokens=512,
        )
        print("Documents successfully inserted into the vector DB")

    except Exception as e:
        print("Embedding insert failed:", e)
        raise Exception(f"Failed to insert documents into vector DB: {e}")

@dsl.component(base_image=BASE_IMAGE)
def generate_provenance(input_dir: dsl.InputPath()):
    import base64
    import datetime
    import gzip
    import hashlib
    import json
    import llama_stack_client
    import io
    import os
    import requests
    import subprocess

    from kubernetes import client, config, stream
    from pathlib import Path

    # Connect to the cluster
    config.load_incluster_config()

    def get_predicate_skeleton() -> dict:
        return {
            "buildDefinition": {
                "buildType": "",
                "externalParameters": {},
                "internalParameters": {},
                "resolvedDependencies": [],
            },
            "runDetails": {
                "builder": {
                    "id": "",
                    "builderDependencies": [],
                    "version": {
                        "embedding_model": os.getenv('EMBEDDING_MODEL'),
                        "llama_stack_client": f"{llama_stack_client.__version__}",
                    },
                },
                "metadata": {
                    "invocationId": "",
                    "startedOn": "",
                    "finishedOn": f"{datetime.datetime.now(datetime.UTC).isoformat()}",
                },
                "byproducts": [],
            }
        }

    def get_db_sha() -> tuple:
        secret_name="pgvector" # Needs to be hardcoded for the moment

        with open("/var/run/secrets/kubernetes.io/serviceaccount/namespace") as f:
            namespace = f.read().strip()
        secret = client.CoreV1Api().read_namespaced_secret(secret_name, namespace)
        container = base64.b64decode(secret.data["host"]).decode("utf-8")
        db_name=base64.b64decode(secret.data["dbname"]).decode("utf-8")
        db_username = base64.b64decode(secret.data["user"]).decode("utf-8")
        pod = f"{container}-0"

        command = [
            "/bin/bash",
            "-c",
            f"pg_dump -U {db_username} -d {db_name} | sha512sum -",
        ]
        # Exec into the container
        resp = stream.stream(
            client.CoreV1Api().connect_get_namespaced_pod_exec,
            namespace=namespace,
            name=pod,
            container=container,
            command=command,
            stderr=True,
            stdin=True,
            stdout=True,
            tty=True,
        )
        return (f"{db_name}", resp.split()[0])

    def get_sources_sha():
        chunk_size = 2**20
        files=[p for p in Path(input_dir).iterdir() if p.is_file()]
        for file in files:
            shasum = hashlib.sha512()
            with file.open("rb") as f:
                for chunk in iter(lambda: f.read(chunk_size), b""):
                    shasum.update(chunk)
            yield (
                str(file).removeprefix(str(input_dir)+os.sep),
                shasum.hexdigest(),
            )

    def get_cosign() -> str:
        # Get URL from which to download the binary
        route = client.CustomObjectsApi().list_namespaced_custom_object(
            group="route.openshift.io",
            version="v1",
            namespace="trusted-artifact-signer",
            plural="routes",
            label_selector="app.kubernetes.io/component=client-server"
        )
        host = route["items"][0]["spec"]["host"]
        url = f"https://{host}/clients/linux/cosign-amd64.gz"

        # Download the binary archive
        dest_path = "/tmp/cosign"
        response = requests.get(url)
        response.raise_for_status()

        # Decompress the archive
        dest_path = "/tmp/cosign"
        with gzip.GzipFile(fileobj=io.BytesIO(response.content)) as f:
            decompressed = f.read()

        # Write the binary to disk and make it executable
        dest_path = "/tmp/cosign"
        with open(dest_path, "wb") as f:
            f.write(decompressed)
        os.chmod(dest_path, 0o755)

        return dest_path

    def get_rekor() -> str:
        route = client.CustomObjectsApi().list_namespaced_custom_object(
            group="route.openshift.io",
            version="v1",
            namespace="trusted-artifact-signer",
            plural="routes",
            label_selector="app.kubernetes.io/component=rekor-server"
        )
        host = route["items"][0]["spec"]["host"]
        return f"https://{host}"

    def get_signing_key() -> str:
        secret = client.CoreV1Api().read_namespaced_secret("signing-secrets", "openshift-pipelines")
        key = base64.b64decode(secret.data["cosign.key"]).decode("utf-8")
        password = base64.b64decode(secret.data["cosign.password"]).decode("utf-8")
        return (key, password)
    
    def run_cosign(command: list):
        cosign_key, cosign_password = get_signing_key()

        result = subprocess.run(
            command,
            capture_output=True,
            env={
                "COSIGN_KEY": cosign_key,
                "COSIGN_PASSWORD": cosign_password
            },
            text=True
        )
        if result.returncode != 0:
            print("Output:")
            print(result.stdout)
            print("Error:")
            print(result.stderr)
            raise RuntimeError("cosign command failed")

    def cosign(predicate: str, blob: str) -> str:
        bin_path = get_cosign()
        rekor_url = get_rekor()

        predicate_path = "/tmp/predicate.json"
        with open(predicate_path, "w") as f:
            f.write(predicate)

        blob_path = "/tmp/blob.txt"
        with open(blob_path, "w") as f:
            f.write(blob)

        run_cosign([
            bin_path,
            "attest-blob",
            blob_path,
            f"--predicate={predicate_path}",
            f"--key=env://COSIGN_KEY",
            f"--rekor-url={rekor_url}",
            "-y",
        ])

        run_cosign([
            bin_path,
            "sign-blob",
            blob_path,
            f"--key=env://COSIGN_KEY",
            f"--rekor-url={rekor_url}",
            "-y",
        ])

        shasum = hashlib.sha256()
        shasum.update(blob.encode())
        return shasum.hexdigest()

    predicate = get_predicate_skeleton()

    # Add subject
    db_name, db_sha = get_db_sha()
    blob = f"{db_name}:{db_sha}"

    # Add sources
    dependencies = predicate["buildDefinition"]["resolvedDependencies"]
    for source, sha in get_sources_sha():
        dependency = {
            "source": source,
            "sha512": sha,
        }
        dependencies.append(dependency)

    # Sign
    predicate_str = json.dumps(predicate, indent=2)
    print()
    print("Predicate:")
    print(predicate_str)
    print()

    sha = cosign(predicate_str, blob)
    print(f"Hash: 'sha256:{sha}'")
    print()
