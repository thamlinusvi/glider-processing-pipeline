FROM ubuntu:latest

# --- Install system dependencies ---
RUN apt-get update && apt-get install -y \
    curl git vim less unzip \
    build-essential libnetcdf-dev libhdf5-dev \
    hdf5-tools netcdf-bin ca-certificates sudo bash wget

# --- Install Miniconda ---
ENV CONDA_DIR=/opt/conda
RUN wget --quiet https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -O /tmp/miniconda.sh && \
    bash /tmp/miniconda.sh -b -p $CONDA_DIR && \
    rm /tmp/miniconda.sh
ENV PATH=$CONDA_DIR/bin:$PATH

# --- Accept Anaconda ToS (required by recent Miniconda versions) ---
RUN conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/main && \
    conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/r

# --- Copy environment file and create conda env ---
COPY environment.yaml /tmp/environment.yaml
RUN conda env create -f /tmp/environment.yaml && conda clean -a
ENV PATH=/opt/conda/envs/glider-env/bin:$PATH
ENV CONDA_DEFAULT_ENV=glider-env

# --- Copy rugliderqc source (with all patches applied) ---
COPY rugliderqc /opt/rugliderqc

# --- Install dependencies, patch ioos_qc, and install rugliderqc ---
RUN pip install ioos_qc PyCO2SYS && \
    cp /opt/rugliderqc/ioos_qc_mods/qartod.py /opt/conda/envs/glider-env/lib/python3.9/site-packages/ioos_qc/qartod.py && \
    mv /opt/rugliderqc/run_glider_qc.py /opt/rugliderqc/rugliderqc/ && \
    pip install /opt/rugliderqc

# --- Create non-root user and working dir ---
RUN useradd -ms /bin/bash glider
USER glider
WORKDIR /home/glider

# --- gncutils is expected at /home/glider/gncutils (bind-mounted from host) ---
# --- rugliderqc is installed at /opt/rugliderqc ---
ENV PYTHONPATH=/home/glider/gncutils:/opt/rugliderqc

CMD ["/bin/bash"]
