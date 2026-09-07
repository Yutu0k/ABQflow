Installation
============

Install Using pip
-----------------

.. code-block:: bash

   pip install ABQflow

If you want the optional ``abqpy`` integration path:

.. code-block:: bash

   pip install "ABQflow[abqpy]"

Additionally, if you want to install the optional dependencies for remote execution:

.. code-block:: bash

   pip install "ABQflow[remote]"

Install Using Pixi
------------------

.. code-block:: bash

   pixi add --pypi ABQflow
   pixi add --pypi "ABQflow[abqpy, remote]"

Dependencies
------------

* **Abaqus** installed and the ``abaqus`` command available on ``PATH``.
* **Python 3.9+**.
* **numpy** for numerical operations.
* **rich** for console output formatting.
* **psutil** for process management.

